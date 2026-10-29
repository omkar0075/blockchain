import { network } from "hardhat";
import { pad, stringToHex } from "viem";

const STATUS_NAMES = [
  "Proposed",
  "Committed",
  "Fulfilled",
  "Cancelled",
  "Expired",
  "Disputed",
] as const;

async function main() {
  console.log("Connecting to network: localhost...");
  const { viem } = await network.connect("localhost");

  const publicClient = await viem.getPublicClient();
  const walletClients = await viem.getWalletClients();

  if (walletClients.length < 2) {
    throw new Error("At least two development accounts are required on the local network.");
  }

  // Account 0 simulates Requester (e.g., Hospital A)
  // Account 1 simulates Provider (e.g., Diagnostic Lab C)
  const [requester, provider] = walletClients;

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x5FbDB2315678afecb367f032d93F642f64180aa3") as `0x${string}`;

  console.log(`Loading MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  // Prepare reservation data
  const latestBlock = await publicClient.getBlock();
  const expiresAt = latestBlock.timestamp + 3600n; // 1 hour in the future
  const resourceRef = pad(stringToHex("diagnostic-mri-slot-101"), { size: 32 });

  console.log("\nSubmitting createReservation transaction...");
  console.log(`  Requester (Hospital A):    ${requester.account.address}`);
  console.log(`  Provider  (Diagnostic Lab): ${provider.account.address}`);
  console.log(`  Resource Reference:        ${resourceRef}`);
  console.log(`  Expires At (Unix):         ${expiresAt}`);

  // 1. Send transaction from requester account
  const txHash = await reservation.write.createReservation(
    [provider.account.address, resourceRef, expiresAt],
    { account: requester.account }
  );

  console.log(`  Transaction Hash:          ${txHash}`);
  console.log("Waiting for block confirmation...");
  const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
  console.log(`  Confirmed in block:        ${receipt.blockNumber}`);

  // 2. Retrieve reservation ID from emitted event
  const events = await publicClient.getContractEvents({
    address: contractAddress,
    abi: reservation.abi,
    eventName: "ReservationCreated",
    fromBlock: receipt.blockNumber,
    strict: true,
  });

  if (!events || events.length === 0) {
    throw new Error("ReservationCreated event was not emitted.");
  }

  const reservationId = (events[0] as any).args.reservationId as `0x${string}`;

  // 3. Read reservation back from on-chain storage
  console.log("\nReading reservation state from contract...");
  const data = await reservation.read.reservations([reservationId]);
  const statusIndex = Number(data[5]);
  const statusName = STATUS_NAMES[statusIndex] || `Unknown (${statusIndex})`;

  console.log("\n========================================================");
  console.log(" Reservation Created and Verified Successfully!");
  console.log("========================================================");
  console.log(` Reservation ID:  ${reservationId}`);
  console.log(` Stored Requester:${data[2]}`);
  console.log(` Stored Provider: ${data[3]}`);
  console.log(` Stored Expiry:   ${data[4]}`);
  console.log(` Current Status:  ${statusName} (code: ${statusIndex})`);
  console.log("========================================================");

  // 4. Verify initial status is Proposed (0)
  if (statusIndex !== 0) {
    throw new Error(`Expected initial status to be Proposed (0), but got ${statusName} (${statusIndex})`);
  }

  console.log(" Status check PASSED: Contract correctly initialized state to Proposed.\n");
}

main().catch((error) => {
  console.error("Reservation creation/verification failed:", error);
  process.exitCode = 1;
});
