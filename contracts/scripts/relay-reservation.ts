import { network } from "hardhat";
import { pad, stringToHex } from "viem";

async function main() {
  const networkName = process.env.HARDHAT_NETWORK || "localhost";
  console.log(`Connecting to network: ${networkName}...`);

  const { viem } = await network.connect(networkName);
  const publicClient = await viem.getPublicClient();
  const walletClients = await viem.getWalletClients();

  if (walletClients.length < 3) {
    throw new Error(
      "At least 3 accounts required on network to demonstrate meta-transactions (Hospital, Provider, Relayer)."
    );
  }

  // Roles:
  // Account 0: Hospital A (Requester, signs off-chain, pays 0 gas)
  // Account 1: Diagnostic Lab C (Provider)
  // Account 2: MEDway Relayer (Backend/Sponsor, broadcasts tx and pays gas)
  const [hospitalA, labC, relayer] = walletClients;

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x5FbDB2315678afecb367f032d93F642f64180aa3") as `0x${string}`;

  console.log(`Loading MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  console.log("\n========================================================");
  console.log(" EIP-712 GASLESS META-TRANSACTION RELAY DEMONSTRATION");
  console.log("========================================================");
  console.log(` Requester (Hospital A):    ${hospitalA.account.address}`);
  console.log(` Provider  (Lab C):         ${labC.account.address}`);
  console.log(` Sponsor / Relayer:         ${relayer.account.address}`);
  console.log("========================================================\n");

  // Step 1: Hospital A constructs and signs the reservation request off-chain
  console.log("Step 1: Hospital A signs reservation authorization off-chain (0 gas)...");
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

  const expiresAt = latestBlock.timestamp + 3600n; // Reservation valid for 1 hour
  const deadline = latestBlock.timestamp + 1800n;  // Signature valid for 30 minutes
  const nonce = await reservation.read.nonces([hospitalA.account.address]);
  const resourceRef = pad(stringToHex("slot-mri-gasless-101"), { size: 32 });

  const signature = await hospitalA.signTypedData({
    domain,
    types,
    primaryType: "ReservationRequest",
    message: {
      requester: hospitalA.account.address,
      provider: labC.account.address,
      resourceRef,
      expiresAt,
      nonce,
      deadline,
    },
  });

  console.log(`  Nonce:     ${nonce}`);
  console.log(`  Signature: ${signature.slice(0, 34)}...`);

  // Step 2: MEDway Relayer submits the signed authorization and sponsors gas
  console.log("\nStep 2: Relayer broadcasts createReservationWithSig and pays gas...");
  const txHash = await reservation.write.createReservationWithSig(
    [
      hospitalA.account.address,
      labC.account.address,
      resourceRef,
      expiresAt,
      deadline,
      signature,
    ],
    { account: relayer.account }
  );

  console.log(`  Transaction Hash:   ${txHash}`);
  console.log("Waiting for confirmation...");
  const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
  console.log(`  Confirmed in block: ${receipt.blockNumber}`);

  // Retrieve reservation ID from events
  const events = await publicClient.getContractEvents({
    address: contractAddress,
    abi: reservation.abi,
    eventName: "ReservationCreated",
    fromBlock: receipt.blockNumber,
    strict: true,
  });

  const reservationId = (events[0] as any).args.reservationId as `0x${string}`;

  // Step 3: Verify the contract state
  console.log("\nStep 3: Checking on-chain reservation records...");
  const data = await reservation.read.reservations([reservationId]);

  console.log("\n========================================================");
  console.log(" RELAYED RESERVATION VERIFICATION RESULTS");
  console.log("========================================================");
  console.log(` Reservation ID:         ${reservationId}`);
  console.log(` Recorded Requester:     ${data[2]}`);
  console.log(` Expected Hospital A:    ${hospitalA.account.address}`);
  console.log(` Gas Sponsor (Relayer):  ${relayer.account.address}`);
  console.log("--------------------------------------------------------");

  const requesterMatchesHospital = data[2].toLowerCase() === hospitalA.account.address.toLowerCase();
  const relayerNotStoredAsHospital = data[2].toLowerCase() !== relayer.account.address.toLowerCase();

  if (requesterMatchesHospital && relayerNotStoredAsHospital) {
    console.log(" Identity Check:         PASSED! Hospital A is accurately recorded.");
    console.log(" Gas Sponsorship:        PASSED! Relayer paid gas without identity overwrite.");
  } else {
    throw new Error("Identity check failed: Requester address does not match Hospital A!");
  }
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("\nRelay demonstration failed:", error?.message || error);
  process.exitCode = 1;
});
