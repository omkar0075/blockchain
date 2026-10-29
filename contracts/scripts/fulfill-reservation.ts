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

  // Account 0 simulates Requester (Hospital A)
  // Account 1 simulates Provider (Diagnostic Lab C)
  const [requester, provider] = walletClients;

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x5FbDB2315678afecb367f032d93F642f64180aa3") as `0x${string}`;

  console.log(`Loading MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  // 1. Identify target reservation ID
  let targetReservationId = process.env.RESERVATION_ID as `0x${string}` | undefined;

  if (!targetReservationId) {
    console.log("Searching for an active Committed reservation...");
    const pastCreated = await publicClient.getContractEvents({
      address: contractAddress,
      abi: reservation.abi,
      eventName: "ReservationCreated",
      fromBlock: 0n,
      strict: true,
    });

    // Check recent reservations in reverse for one in Committed status (1)
    for (let i = pastCreated.length - 1; i >= 0; i--) {
      const candidateId = (pastCreated[i] as any).args.reservationId as `0x${string}`;
      const candidateData = await reservation.read.reservations([candidateId]);
      if (Number(candidateData[5]) === 1) {
        targetReservationId = candidateId;
        break;
      }
    }

    // If none currently committed, check if there's a Proposed one to accept, or create fresh
    if (!targetReservationId) {
      console.log("No Committed reservation found. Preparing a reservation to commit first...");

      // Find any proposed reservation
      let proposedId: `0x${string}` | undefined;
      for (let i = pastCreated.length - 1; i >= 0; i--) {
        const candidateId = (pastCreated[i] as any).args.reservationId as `0x${string}`;
        const candidateData = await reservation.read.reservations([candidateId]);
        if (Number(candidateData[5]) === 0) {
          proposedId = candidateId;
          break;
        }
      }

      if (!proposedId) {
        console.log("Creating fresh proposal...");
        const block = await publicClient.getBlock();
        const expiresAt = block.timestamp + 3600n;
        const resourceRef = pad(stringToHex("slot-mri-scan-lab-c"), { size: 32 });

        const createTx = await reservation.write.createReservation(
          [provider.account.address, resourceRef, expiresAt],
          { account: requester.account }
        );
        const receipt = await publicClient.waitForTransactionReceipt({ hash: createTx });
        const createdEvents = await publicClient.getContractEvents({
          address: contractAddress,
          abi: reservation.abi,
          eventName: "ReservationCreated",
          fromBlock: receipt.blockNumber,
          strict: true,
        });
        proposedId = (createdEvents[0] as any).args.reservationId as `0x${string}`;
      }

      console.log(`Accepting reservation ${proposedId} as provider...`);
      const acceptTx = await reservation.write.acceptReservation([proposedId], {
        account: provider.account,
      });
      await publicClient.waitForTransactionReceipt({ hash: acceptTx });
      targetReservationId = proposedId;
    }
  }

  console.log(`\nTarget Reservation ID: ${targetReservationId}`);

  // Check state before fulfillment
  const beforeData = await reservation.read.reservations([targetReservationId]);
  const beforeStatus = Number(beforeData[5]);
  console.log(`Status before fulfillment: ${STATUS_NAMES[beforeStatus]} (code: ${beforeStatus})`);
  console.log(`Designated Provider:       ${beforeData[3]}`);
  console.log(`Fulfillment Signer:        ${provider.account.address}`);

  if (beforeStatus !== 1) {
    throw new Error(
      `Reservation must be Committed (1) to be fulfilled, but current status is ${STATUS_NAMES[beforeStatus]} (${beforeStatus})`
    );
  }

  // 2. Call fulfillReservation from the designated provider account
  console.log("\nSubmitting fulfillReservation transaction as provider...");
  const txHash = await reservation.write.fulfillReservation([targetReservationId], {
    account: provider.account,
  });

  console.log(`  Transaction Hash:        ${txHash}`);
  console.log("Waiting for block confirmation...");
  const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
  console.log(`  Confirmed in block:      ${receipt.blockNumber}`);

  // 3. Read back on-chain state to verify transition to Fulfilled (2)
  const afterData = await reservation.read.reservations([targetReservationId]);
  const afterStatusCode = Number(afterData[5]);
  const afterStatusName = STATUS_NAMES[afterStatusCode] || `Unknown (${afterStatusCode})`;
  const fulfilledAt = afterData[7];

  console.log("\n========================================================");
  console.log(" Reservation Fulfilled Successfully!");
  console.log("========================================================");
  console.log(` Reservation ID:    ${targetReservationId}`);
  console.log(` Provider:          ${afterData[3]}`);
  console.log(` Requester:         ${afterData[2]}`);
  console.log(` New Status:        ${afterStatusName} (code: ${afterStatusCode})`);
  console.log(` Fulfilled At:      ${fulfilledAt} (Unix timestamp)`);
  console.log("========================================================");

  if (afterStatusCode !== 2) {
    throw new Error(
      `Expected status to be Fulfilled (2), but got ${afterStatusName} (${afterStatusCode})`
    );
  }

  console.log(" Status check PASSED: Status transitioned from Committed (1) -> Fulfilled (2).\n");
}

main().catch((error) => {
  console.error("Reservation fulfillment failed:", error);
  process.exitCode = 1;
});

