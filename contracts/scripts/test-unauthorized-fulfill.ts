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

  if (walletClients.length < 3) {
    throw new Error("At least three development accounts are required to test third-party security.");
  }

  // Account 0: Requester (Hospital A)
  // Account 1: Legitimate Provider (Diagnostic Lab C)
  // Account 2: Unrelated Third Party (Attacker / Random Account)
  const [requester, legitimateProvider, attacker] = walletClients;

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x5FbDB2315678afecb367f032d93F642f64180aa3") as `0x${string}`;

  console.log(`Loading MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  console.log("\n========================================================");
  console.log(" SECURITY TEST: Unauthorized Fulfillment Prevention");
  console.log("========================================================");
  console.log(` Requester (Hospital A):           ${requester.account.address}`);
  console.log(` Legitimate Provider (Lab C):      ${legitimateProvider.account.address}`);
  console.log(` Unauthorized Outsider (Attacker): ${attacker.account.address}`);
  console.log("========================================================\n");

  // Step 1: Set up a committed reservation between Hospital A and Lab C
  console.log("Step 1: Setting up an active Committed reservation...");
  const block = await publicClient.getBlock();
  const expiresAt = block.timestamp + 3600n;
  const resourceRef = pad(stringToHex("slot-mri-security-test"), { size: 32 });

  const createTx = await reservation.write.createReservation(
    [legitimateProvider.account.address, resourceRef, expiresAt],
    { account: requester.account }
  );
  const createReceipt = await publicClient.waitForTransactionReceipt({ hash: createTx });
  const createdEvents = await publicClient.getContractEvents({
    address: contractAddress,
    abi: reservation.abi,
    eventName: "ReservationCreated",
    fromBlock: createReceipt.blockNumber,
    strict: true,
  });
  const reservationId = (createdEvents[0] as any).args.reservationId as `0x${string}`;
  console.log(`  Reservation created: ${reservationId}`);

  // Lab C commits to the reservation
  const acceptTx = await reservation.write.acceptReservation([reservationId], {
    account: legitimateProvider.account,
  });
  await publicClient.waitForTransactionReceipt({ hash: acceptTx });
  console.log("  Reservation accepted by legitimate provider. Status is now Committed (1).");

  // Step 2: Attempt fulfillment as an unauthorized third party
  console.log("\nStep 2: Simulating attack — unauthorized account attempts fulfillReservation()...");
  console.log(`  Signer: ${attacker.account.address} (NOT the designated provider)`);

  let attackBlocked = false;
  try {
    // Attempt fulfillment with attacker account
    await reservation.write.fulfillReservation([reservationId], {
      account: attacker.account,
    });

    throw new Error(
      "CRITICAL SECURITY VULNERABILITY: An unauthorized third party was able to fulfill the reservation!"
    );
  } catch (error: any) {
    const errorMsg = error?.message || String(error);
    if (errorMsg.includes("Unauthorized") || errorMsg.includes("revert")) {
      attackBlocked = true;
      console.log("  [BLOCKED] Transaction reverted with 'Unauthorized()' error as expected!");
    } else {
      // Re-throw if unexpected error
      throw error;
    }
  }

  if (!attackBlocked) {
    throw new Error("Security check failed: Attack was not properly blocked.");
  }

  // Step 3: Verify the reservation status on chain was untouched
  console.log("\nStep 3: Checking on-chain reservation status after attack...");
  const afterData = await reservation.read.reservations([reservationId]);
  const currentStatusCode = Number(afterData[5]);
  const currentStatusName = STATUS_NAMES[currentStatusCode];

  console.log(`  On-chain status: ${currentStatusName} (code: ${currentStatusCode})`);
  console.log(`  FulfilledAt:     ${afterData[7]}`);

  if (currentStatusCode !== 1) {
    throw new Error(
      `State integrity failed: Expected status to remain Committed (1), but found ${currentStatusName} (${currentStatusCode})`
    );
  }

  console.log("\n========================================================");
  console.log(" SECURITY VERIFICATION PASSED!");
  console.log("========================================================");
  console.log(" The contract strictly enforces role-based access control:");
  console.log(" - Only the designated provider can fulfill the reservation.");
  console.log(" - Random third-party accounts are rejected with Unauthorized().");
  console.log(" - Healthcare commitment records remain uncompromised.");
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("Security test failed:", error);
  process.exitCode = 1;
});

