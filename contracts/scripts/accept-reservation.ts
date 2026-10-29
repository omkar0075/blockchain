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

  const [requester, provider] = walletClients;

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x5FbDB2315678afecb367f032d93F642f64180aa3") as `0x${string}`;

  console.log(`Loading MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  // 1. Identify target reservation ID
  let targetReservationId = process.env.RESERVATION_ID as `0x${string}` | undefined;

  if (!targetReservationId) {
    console.log("Looking up latest created reservation from event logs...");
    const pastEvents = await publicClient.getContractEvents({
      address: contractAddress,
      abi: reservation.abi,
      eventName: "ReservationCreated",
      fromBlock: 0n,
      strict: true,
    });

    // Find the latest reservation that is still in Proposed status
    for (let i = pastEvents.length - 1; i >= 0; i--) {
      const candidateId = (pastEvents[i] as any).args.reservationId as `0x${string}`;
      const candidateData = await reservation.read.reservations([candidateId]);
      if (Number(candidateData[5]) === 0) {
        targetReservationId = candidateId;
        break;
      }
    }

    // If no open Proposed reservation exists, create a new one first
    if (!targetReservationId) {
      console.log("No pending Proposed reservation found. Creating a fresh proposal...");
      const block = await publicClient.getBlock();
      const expiresAt = block.timestamp + 3600n;
      const resourceRef = pad(stringToHex("slot-ct-scan-hospital-a"), { size: 32 });

      const createTx = await reservation.write.createReservation(
        [provider.account.address, resourceRef, expiresAt],
        { account: requester.account }
      );
      const receipt = await publicClient.waitForTransactionReceipt({ hash: createTx });

      const newEvents = await publicClient.getContractEvents({
        address: contractAddress,
        abi: reservation.abi,
        eventName: "ReservationCreated",
        fromBlock: receipt.blockNumber,
        strict: true,
      });

      targetReservationId = (newEvents[0] as any).args.reservationId as `0x${string}`;
      console.log(`Fresh reservation created: ${targetReservationId}`);
    }
  }

  console.log(`\nTarget Reservation ID: ${targetReservationId}`);

  // Check state before acceptance
  const beforeData = await reservation.read.reservations([targetReservationId]);
  console.log(`State before accept:   ${STATUS_NAMES[Number(beforeData[5])]} (code: ${beforeData[5]})`);
  console.log(`Designated Provider:   ${beforeData[3]}`);
  console.log(`Signer (Account #1):   ${provider.account.address}`);

  // 2. Call acceptReservation from the designated provider account
  console.log("\nSubmitting acceptReservation transaction as provider...");
  const txHash = await reservation.write.acceptReservation([targetReservationId], {
    account: provider.account,
  });

  console.log(`  Transaction Hash:    ${txHash}`);
  console.log("Waiting for block confirmation...");
  const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
  console.log(`  Confirmed in block:  ${receipt.blockNumber}`);

  // 3. Read back on-chain state to verify status changed to Committed (1)
  const afterData = await reservation.read.reservations([targetReservationId]);
  const afterStatusCode = Number(afterData[5]);
  const afterStatusName = STATUS_NAMES[afterStatusCode] || `Unknown (${afterStatusCode})`;

  console.log("\n========================================================");
  console.log(" Reservation Accepted and Committed Successfully!");
  console.log("========================================================");
  console.log(` Reservation ID:    ${targetReservationId}`);
  console.log(` Provider:          ${afterData[3]}`);
  console.log(` Requester:         ${afterData[2]}`);
  console.log(` New Status:        ${afterStatusName} (code: ${afterStatusCode})`);
  console.log("========================================================");

  if (afterStatusCode !== 1) {
    throw new Error(`Expected status to be Committed (1), but got ${afterStatusName} (${afterStatusCode})`);
  }

  console.log(" Status check PASSED: Status transitioned from Proposed (0) -> Committed (1).\n");
}

main().catch((error) => {
  console.error("Reservation acceptance failed:", error);
  process.exitCode = 1;
});

