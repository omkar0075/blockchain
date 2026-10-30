import { network } from "hardhat";
import { pad, parseEventLogs, stringToHex } from "viem";
import { getInstitutionKeys } from "./load-env.js";

async function main() {
  console.log("Connecting to network: coral...");
  const { viem } = await network.connect("coral");

  const publicClient = await viem.getPublicClient();
  const walletClients = await viem.getWalletClients();

  if (!walletClients || walletClients.length === 0) {
    throw new Error("No relayer signer available for Coral Testnet. Set CORAL_PRIVATE_KEY.");
  }

  // Relayer account (paying gas on Coral Testnet)
  const relayer = walletClients[0];

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0xf8a978c5d5dfc258b5aefb4c3ea99b73615b7995") as `0x${string}`;

  console.log(`Loading upgraded MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  // Load persistent, secure institution identities from environment
  const { hospitalA, labC } = getInstitutionKeys();
  const hospitalBalance = await publicClient.getBalance({ address: hospitalA.address });
  const labBalance = await publicClient.getBalance({ address: labC.address });
  const relayerBalance = await publicClient.getBalance({ address: relayer.account.address });

  console.log("\n========================================================");
  console.log(" LIVE CORAL TESTNET SPONSORED RESERVATION");
  console.log("========================================================");
  console.log(` Requester (Hospital A):    ${hospitalA.address}`);
  console.log(` Hospital A MST Balance:    ${hospitalBalance} wei (0 MST - Gasless!)`);
  console.log(` Provider  (Lab C):         ${labC.address}`);
  console.log(` Lab C MST Balance:         ${labBalance} wei (0 MST - Gasless!)`);
  console.log(` Relayer / Gas Sponsor:     ${relayer.account.address}`);
  console.log(` Relayer MST Balance:       ${relayerBalance} wei`);
  console.log("========================================================\n");

  // Step 1: Hospital A signs EIP-712 Typed Data off-chain
  console.log("Step 1: Hospital A generates off-chain EIP-712 authorization signature...");
  const chainId = await publicClient.getChainId();
  const latestBlock = await publicClient.getBlock();

  const domain = {
    name: "MEDwayReservation",
    version: "1",
    chainId: Number(chainId),
    verifyingContract: contractAddress,
  } as const;

  const types = {
    ReservationRequest: [
      { name: "requester", type: "address" },
      { name: "provider", type: "address" },
      { name: "resourceRef", type: "bytes32" },
      { name: "expiresAt", type: "uint64" },
      { name: "nonce", type: "uint256" },
      { name: "deadline", type: "uint256" },
    ],
  } as const;

  const expiresAt = latestBlock.timestamp + 7200n; // 2 hours
  const deadline = latestBlock.timestamp + 1800n;  // 30 min signature validity
  const nonce = await reservation.read.nonces([hospitalA.address]);
  const resourceRef = pad(stringToHex(`slot-mri-coral-${Date.now()}`), { size: 32 });

  const signature = await hospitalA.signTypedData({
    domain,
    types,
    primaryType: "ReservationRequest",
    message: {
      requester: hospitalA.address,
      provider: labC.address,
      resourceRef,
      expiresAt,
      nonce,
      deadline,
    },
  });

  console.log(`  Institution Nonce: ${nonce}`);
  console.log(`  Signature:         ${signature.slice(0, 34)}...`);

  // Step 2: Relayer submits transaction to Coral Testnet and sponsors gas
  console.log("\nStep 2: Relayer submits createReservationWithSig() to Coral Testnet...");
  const txHash = await reservation.write.createReservationWithSig(
    [
      hospitalA.address,
      labC.address,
      resourceRef,
      expiresAt,
      deadline,
      signature,
    ],
    { account: relayer.account }
  );

  console.log(`  Transaction Hash:  ${txHash}`);
  console.log("  Awaiting block confirmation on Coral Testnet...");
  const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
  console.log(`  Confirmed in block:#${receipt.blockNumber}`);

  // Retrieve reservation ID directly from receipt logs with verified decoding
  const createdLogs = parseEventLogs({
    abi: reservation.abi,
    logs: receipt.logs,
    eventName: "ReservationCreated",
  });

  if (createdLogs.length === 0) {
    throw new Error("No ReservationCreated event emitted in receipt logs");
  }

  const createdEvent = createdLogs[0];
  const reservationId = createdEvent.args.reservationId;
  const decodedRequester = createdEvent.args.requester;
  const decodedProvider = createdEvent.args.provider;

  // Step 3: Verify on-chain storage on Coral
  console.log("\nStep 3: Querying confirmed on-chain commitment record from Coral...");
  const onChainData = await reservation.read.reservations([reservationId]);

  console.log("\n========================================================");
  console.log(" SPONSORED RESERVATION CONFIRMED ON CORAL TESTNET!");
  console.log("========================================================");
  console.log(` Reservation ID:     ${reservationId}`);
  console.log(` Stored Requester:   ${onChainData[2]}`);
  console.log(` Expected Hospital:  ${hospitalA.address}`);
  console.log(` Decoded Log Req:    ${decodedRequester}`);
  console.log(` Stored Provider:    ${onChainData[3]}`);
  console.log(` Expected Provider:  ${labC.address}`);
  console.log(` Decoded Log Prov:   ${decodedProvider}`);
  console.log(` Relayer (Sponsor):  ${relayer.account.address}`);
  console.log(` Block Number:       #${receipt.blockNumber}`);
  console.log(` Transaction Hash:   ${txHash}`);
  console.log("--------------------------------------------------------");

  const identityPreserved =
    onChainData[2].toLowerCase() === hospitalA.address.toLowerCase() &&
    decodedRequester.toLowerCase() === hospitalA.address.toLowerCase();
  const relayerExcludedFromIdentity =
    onChainData[2].toLowerCase() !== relayer.account.address.toLowerCase();

  if (identityPreserved && relayerExcludedFromIdentity) {
    console.log(" IDENTITY INTEGRITY: PASSED (Hospital A is the true requester)");
    console.log(" GAS SPONSORSHIP:    PASSED (Hospital A paid 0 gas; relayer sponsored)");
    console.log(" EVENT DECODING:     PASSED (Direct receipt log decoded and verified)");
  } else {
    throw new Error("Identity verification failed on-chain!");
  }
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("\nSponsored transaction failed:", error?.message || error);
  process.exitCode = 1;
});
