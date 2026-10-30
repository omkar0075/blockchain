import { network } from "hardhat";
import { pad, parseEventLogs, stringToHex } from "viem";
import { getInstitutionKeys } from "./load-env.js";

const STATUS_NAMES = [
  "Proposed",
  "Committed",
  "Fulfilled",
  "Cancelled",
  "Expired",
  "Disputed",
] as const;

async function main() {
  const networkName = process.env.HARDHAT_NETWORK || "coral";
  console.log(`Connecting to network: ${networkName}...`);

  const { viem } = await network.connect(networkName);
  const publicClient = await viem.getPublicClient();
  const walletClients = await viem.getWalletClients();

  if (!walletClients || walletClients.length === 0) {
    throw new Error("No relayer signer available. Set CORAL_PRIVATE_KEY.");
  }

  // 1. Relayer account (sponsors gas on Coral Testnet)
  const relayer = walletClients[0];

  // 2. Persistent, cryptographically secure institution identities
  // Loaded from environment variables (contracts/.env or backend/.env)
  // Completely isolated from relayer account and free from insecure public seed derivation.
  const { hospitalA, labC } = getInstitutionKeys();

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0xf8a978c5d5dfc258b5aefb4c3ea99b73615b7995") as `0x${string}`;

  console.log(`Loading upgraded MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  const [hospBal, labBal, relayerBal] = await Promise.all([
    publicClient.getBalance({ address: hospitalA.address }),
    publicClient.getBalance({ address: labC.address }),
    publicClient.getBalance({ address: relayer.account.address }),
  ]);

  console.log("\n========================================================");
  console.log(" FULL END-TO-END GASLESS PARTICIPATION (EIP-712)");
  console.log("========================================================");
  console.log(` Requester (Hospital A):    ${hospitalA.address}`);
  console.log(` Hospital A MST Balance:    ${hospBal} wei (Gasless Participant)`);
  console.log(` Provider  (Lab C):         ${labC.address}`);
  console.log(` Lab C MST Balance:         ${labBal} wei (Gasless Participant)`);
  console.log(` Relayer   (Gas Sponsor):   ${relayer.account.address}`);
  console.log(` Relayer Balance:           ${relayerBal} wei`);
  console.log("========================================================\n");

  const chainId = await publicClient.getChainId();
  const domain = {
    name: "MEDwayReservation",
    version: "1",
    chainId: Number(chainId),
    verifyingContract: contractAddress,
  } as const;

  // -------------------------------------------------------------------
  // PHASE 1: Gasless Reservation Creation (Hospital A)
  // -------------------------------------------------------------------
  console.log("--------------------------------------------------------");
  console.log("PHASE 1: Hospital A signs reservation proposal off-chain");
  console.log("--------------------------------------------------------");

  const latestBlock = await publicClient.getBlock();
  const expiresAt = latestBlock.timestamp + 7200n; // 2 hours validity
  const createDeadline = latestBlock.timestamp + 1800n; // 30 min signature deadline
  const hospitalNonce = await reservation.read.nonces([hospitalA.address]);
  const resourceRef = pad(stringToHex(`slot-ct-scan-${Date.now()}`), { size: 32 });

  const reservationRequestTypes = {
    ReservationRequest: [
      { name: "requester", type: "address" },
      { name: "provider", type: "address" },
      { name: "resourceRef", type: "bytes32" },
      { name: "expiresAt", type: "uint64" },
      { name: "nonce", type: "uint256" },
      { name: "deadline", type: "uint256" },
    ],
  } as const;

  console.log("Hospital A signs EIP-712 typed data (cost: 0 gas)...");
  const createSignature = await hospitalA.signTypedData({
    domain,
    types: reservationRequestTypes,
    primaryType: "ReservationRequest",
    message: {
      requester: hospitalA.address,
      provider: labC.address,
      resourceRef,
      expiresAt,
      nonce: hospitalNonce,
      deadline: createDeadline,
    },
  });

  console.log(`  Hospital Nonce:    ${hospitalNonce}`);
  console.log(`  Signature:         ${createSignature.slice(0, 34)}...`);

  console.log("\nRelayer submits createReservationWithSig() and sponsors gas...");
  const createTxHash = await reservation.write.createReservationWithSig(
    [
      hospitalA.address,
      labC.address,
      resourceRef,
      expiresAt,
      createDeadline,
      createSignature,
    ],
    { account: relayer.account }
  );

  console.log(`  Tx Hash:           ${createTxHash}`);
  const createReceipt = await publicClient.waitForTransactionReceipt({ hash: createTxHash });
  console.log(`  Confirmed in block:#${createReceipt.blockNumber}`);

  // Hardened Event Decoding: decode directly from createReceipt.logs
  const createdLogs = parseEventLogs({
    abi: reservation.abi,
    logs: createReceipt.logs,
    eventName: "ReservationCreated",
  });

  if (createdLogs.length === 0) {
    throw new Error("No ReservationCreated event found in transaction receipt logs!");
  }

  const createdEvent = createdLogs[0];
  const reservationId = createdEvent.args.reservationId;
  const eventRequester = createdEvent.args.requester;
  const eventProvider = createdEvent.args.provider;
  const eventResourceRef = createdEvent.args.resourceRef;

  console.log(`  Created Reservation ID: ${reservationId}`);
  console.log(`  Decoded Event Requester:${eventRequester}`);
  console.log(`  Decoded Event Provider: ${eventProvider}`);

  // Strict verification of event arguments against expected identities
  if (eventRequester.toLowerCase() !== hospitalA.address.toLowerCase()) {
    throw new Error(
      `Event Requester mismatch! Expected: ${hospitalA.address}, Got: ${eventRequester}`
    );
  }
  if (eventProvider.toLowerCase() !== labC.address.toLowerCase()) {
    throw new Error(
      `Event Provider mismatch! Expected: ${labC.address}, Got: ${eventProvider}`
    );
  }
  if (eventResourceRef !== resourceRef) {
    throw new Error(
      `Event ResourceRef mismatch! Expected: ${resourceRef}, Got: ${eventResourceRef}`
    );
  }

  // Verify Phase 1 State
  const phase1Data = await reservation.read.reservations([reservationId]);
  console.log(`  On-Chain Requester: ${phase1Data[2]} (Hospital A)`);
  console.log(`  On-Chain Status:    ${STATUS_NAMES[Number(phase1Data[5])]} (0)`);

  if (phase1Data[2].toLowerCase() !== hospitalA.address.toLowerCase()) {
    throw new Error("Phase 1 verification failed: Hospital A was not set as requester!");
  }

  // -------------------------------------------------------------------
  // PHASE 2: Gasless Provider Acceptance (Lab C)
  // -------------------------------------------------------------------
  console.log("\n--------------------------------------------------------");
  console.log("PHASE 2: Lab C signs acceptance authorization off-chain");
  console.log("--------------------------------------------------------");

  const labNonce = await reservation.read.nonces([labC.address]);
  const acceptBlock = await publicClient.getBlock();
  const acceptDeadline = acceptBlock.timestamp + 1800n;

  const acceptRequestTypes = {
    AcceptRequest: [
      { name: "reservationId", type: "bytes32" },
      { name: "provider", type: "address" },
      { name: "nonce", type: "uint256" },
      { name: "deadline", type: "uint256" },
    ],
  } as const;

  console.log("Lab C signs AcceptRequest EIP-712 typed data (cost: 0 gas)...");
  const acceptSignature = await labC.signTypedData({
    domain,
    types: acceptRequestTypes,
    primaryType: "AcceptRequest",
    message: {
      reservationId,
      provider: labC.address,
      nonce: labNonce,
      deadline: acceptDeadline,
    },
  });

  console.log(`  Lab C Nonce:       ${labNonce}`);
  console.log(`  Accept Signature:  ${acceptSignature.slice(0, 34)}...`);

  console.log("\nRelayer submits acceptReservationWithSig() and sponsors gas...");
  const acceptTxHash = await reservation.write.acceptReservationWithSig(
    [reservationId, acceptDeadline, acceptSignature],
    { account: relayer.account }
  );

  console.log(`  Tx Hash:           ${acceptTxHash}`);
  const acceptReceipt = await publicClient.waitForTransactionReceipt({ hash: acceptTxHash });
  console.log(`  Confirmed in block:#${acceptReceipt.blockNumber}`);

  // Hardened Event Decoding: decode directly from acceptReceipt.logs
  const acceptLogs = parseEventLogs({
    abi: reservation.abi,
    logs: acceptReceipt.logs,
    eventName: "ReservationCommitted",
  });

  if (acceptLogs.length === 0) {
    throw new Error("No ReservationCommitted event found in accept transaction receipt logs!");
  }

  const committedReservationId = acceptLogs[0].args.reservationId;
  if (committedReservationId.toLowerCase() !== reservationId.toLowerCase()) {
    throw new Error(
      `Committed ID mismatch! Expected: ${reservationId}, Got: ${committedReservationId}`
    );
  }
  console.log(`  Decoded Event Committed ID: ${committedReservationId}`);

  // -------------------------------------------------------------------
  // PHASE 3: On-Chain Verification
  // -------------------------------------------------------------------
  console.log("\n--------------------------------------------------------");
  console.log("PHASE 3: Final On-Chain Verification");
  console.log("--------------------------------------------------------");

  const finalData = await reservation.read.reservations([reservationId]);
  const finalStatusCode = Number(finalData[5]);
  const finalStatusName = STATUS_NAMES[finalStatusCode];

  console.log(`  Reservation ID:        ${reservationId}`);
  console.log(`  Requester:             ${finalData[2]}`);
  console.log(`  Provider:              ${finalData[3]}`);
  console.log(`  Status:                ${finalStatusName} (code: ${finalStatusCode})`);

  if (finalStatusCode !== 1) {
    throw new Error(`Expected status to be Committed (1), but got ${finalStatusName} (${finalStatusCode})`);
  }

  console.log("\n========================================================");
  console.log(" SUCCESS: FULL DUAL-SIDED GASLESS FLOW VERIFIED!");
  console.log("========================================================");
  console.log(" - Hospital A created the reservation proposal with 0 gas.");
  console.log(" - Lab C accepted the commitment with 0 gas.");
  console.log(" - Both institutions used secure, persistent private keys from environment.");
  console.log(" - Receipt logs were directly decoded and validated for both events.");
  console.log(" - Relayer paid all blockchain network fees.");
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("\n[FAILED] Gasless lifecycle test failed:", error?.message || error);
  process.exitCode = 1;
});
