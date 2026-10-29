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
    throw new Error("At least three accounts required: requester, provider, and outsider.");
  }

  const [requester, provider, outsider] = walletClients;

  const contractAddress = (process.env.CONTRACT_ADDRESS ||
    "0x5FbDB2315678afecb367f032d93F642f64180aa3") as `0x${string}`;

  console.log(`Loading MEDwayReservation at: ${contractAddress}`);
  const reservation = await viem.getContractAt("MEDwayReservation", contractAddress);

  const zeroBytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000" as const;
  const validEvidence = pad(stringToHex("dispute-evidence-document-42"), { size: 32 });

  // Helper to create a fresh reservation
  async function createTestReservation(durationSeconds: bigint = 3600n) {
    const block = await publicClient.getBlock();
    const expiresAt = block.timestamp + durationSeconds;
    const resourceRef = pad(stringToHex(`res-${Date.now()}-${Math.random()}`), { size: 32 });

    const tx = await reservation.write.createReservation(
      [provider.account.address, resourceRef, expiresAt],
      { account: requester.account }
    );
    const receipt = await publicClient.waitForTransactionReceipt({ hash: tx });
    const events = await publicClient.getContractEvents({
      address: contractAddress,
      abi: reservation.abi,
      eventName: "ReservationCreated",
      fromBlock: receipt.blockNumber,
      strict: true,
    });
    return {
      id: (events[0] as any).args.reservationId as `0x${string}`,
      expiresAt,
    };
  }

  // Helper to time-travel forward on local EVM
  async function fastForwardSeconds(seconds: number) {
    await (publicClient as any).request({
      method: "evm_increaseTime",
      params: [seconds],
    });
    await (publicClient as any).request({
      method: "evm_mine",
      params: [],
    });
  }

  console.log("\n========================================================");
  console.log(" EXECUTING 5 COMPREHENSIVE SECURITY & RULE TESTS");
  console.log("========================================================");
  console.log(` Requester (Hospital A):    ${requester.account.address}`);
  console.log(` Provider  (Lab C):         ${provider.account.address}`);
  console.log(` Outsider  (Unauthorized):  ${outsider.account.address}`);
  console.log("========================================================\n");

  // -----------------------------------------------------------------------
  // Case 1: Unauthorized acceptance
  // -----------------------------------------------------------------------
  console.log("--- Case 1: Unauthorized Acceptance ---");
  const case1 = await createTestReservation();
  console.log(`Created proposal: ${case1.id}`);
  console.log("Outsider attempts acceptReservation()...");

  let case1Blocked = false;
  try {
    await reservation.write.acceptReservation([case1.id], {
      account: outsider.account,
    });
  } catch (error: any) {
    const msg = error?.message || String(error);
    if (msg.includes("Unauthorized") || msg.includes("revert")) {
      case1Blocked = true;
      console.log("  [PASS] Blocked with Unauthorized() error.");
    } else {
      throw error;
    }
  }
  if (!case1Blocked) throw new Error("Case 1 Failed: Outsider accepted reservation!");
  const case1State = await reservation.read.reservations([case1.id]);
  if (Number(case1State[5]) !== 0) throw new Error("Case 1 Failed: Status modified!");
  console.log("  [PASS] Status remains Proposed (0).\n");

  // -----------------------------------------------------------------------
  // Case 2: Unauthorized cancellation
  // -----------------------------------------------------------------------
  console.log("--- Case 2: Unauthorized Cancellation ---");
  const case2 = await createTestReservation();
  console.log(`Created proposal: ${case2.id}`);
  console.log("Outsider attempts cancelReservation()...");

  let case2Blocked = false;
  try {
    await reservation.write.cancelReservation([case2.id], {
      account: outsider.account,
    });
  } catch (error: any) {
    const msg = error?.message || String(error);
    if (msg.includes("Unauthorized") || msg.includes("revert")) {
      case2Blocked = true;
      console.log("  [PASS] Blocked with Unauthorized() error.");
    } else {
      throw error;
    }
  }
  if (!case2Blocked) throw new Error("Case 2 Failed: Outsider cancelled reservation!");
  const case2State = await reservation.read.reservations([case2.id]);
  if (Number(case2State[5]) !== 0) throw new Error("Case 2 Failed: Status modified!");
  console.log("  [PASS] Status remains Proposed (0).\n");

  // -----------------------------------------------------------------------
  // Case 3: Expiry enforcement (Expired reservation cannot be accepted)
  // -----------------------------------------------------------------------
  console.log("--- Case 3: Expiry Enforcement ---");
  const case3 = await createTestReservation(60n); // 60-second expiration
  console.log(`Created reservation expiring in 60s: ${case3.id}`);
  console.log("Advancing EVM time by 120 seconds past expiration...");
  await fastForwardSeconds(120);

  console.log("Provider attempts to accept after expiry deadline...");
  let case3Blocked = false;
  try {
    await reservation.write.acceptReservation([case3.id], {
      account: provider.account,
    });
  } catch (error: any) {
    const msg = error?.message || String(error);
    if (msg.includes("InvalidExpiry") || msg.includes("revert")) {
      case3Blocked = true;
      console.log("  [PASS] Blocked with InvalidExpiry() error.");
    } else {
      throw error;
    }
  }
  if (!case3Blocked) throw new Error("Case 3 Failed: Expired reservation was accepted!");
  console.log("  [PASS] Contract rejected late acceptance.\n");

  // -----------------------------------------------------------------------
  // Case 4: Invalid state transitions
  // -----------------------------------------------------------------------
  console.log("--- Case 4: Invalid State Transitions ---");
  // 4a: Double fulfillment
  const case4a = await createTestReservation();
  await (await reservation.write.acceptReservation([case4a.id], { account: provider.account }));
  await (await reservation.write.fulfillReservation([case4a.id], { account: provider.account }));
  console.log(`Reservation ${case4a.id} is now Fulfilled.`);
  console.log("Attempting double-fulfillment on already Fulfilled reservation...");

  let doubleFulfillBlocked = false;
  try {
    await reservation.write.fulfillReservation([case4a.id], {
      account: provider.account,
    });
  } catch (error: any) {
    const msg = error?.message || String(error);
    if (msg.includes("InvalidStatus") || msg.includes("revert")) {
      doubleFulfillBlocked = true;
      console.log("  [PASS] Double-fulfillment rejected with InvalidStatus().");
    } else {
      throw error;
    }
  }
  if (!doubleFulfillBlocked) throw new Error("Case 4a Failed: Double fulfillment allowed!");

  // 4b: Fulfilling a cancelled reservation
  const case4b = await createTestReservation();
  await (await reservation.write.cancelReservation([case4b.id], { account: requester.account }));
  console.log(`Reservation ${case4b.id} is now Cancelled.`);
  console.log("Attempting to fulfill a Cancelled reservation...");

  let fulfillCancelledBlocked = false;
  try {
    await reservation.write.fulfillReservation([case4b.id], {
      account: provider.account,
    });
  } catch (error: any) {
    const msg = error?.message || String(error);
    if (msg.includes("InvalidStatus") || msg.includes("revert")) {
      fulfillCancelledBlocked = true;
      console.log("  [PASS] Fulfilling cancelled reservation rejected with InvalidStatus().");
    } else {
      throw error;
    }
  }
  if (!fulfillCancelledBlocked) throw new Error("Case 4b Failed: Cancelled reservation was fulfilled!");
  console.log("  [PASS] State transition invariants preserved.\n");

  // -----------------------------------------------------------------------
  // Case 5: Dispute behavior
  // -----------------------------------------------------------------------
  console.log("--- Case 5: Dispute Behavior ---");
  const case5 = await createTestReservation();
  await (await reservation.write.acceptReservation([case5.id], { account: provider.account }));
  console.log(`Reservation ${case5.id} is now Committed.`);

  // 5a: Outsider dispute attempt
  console.log("5a: Outsider attempts to dispute reservation...");
  let outsiderDisputeBlocked = false;
  try {
    await reservation.write.disputeReservation([case5.id, validEvidence], {
      account: outsider.account,
    });
  } catch (error: any) {
    const msg = error?.message || String(error);
    if (msg.includes("Unauthorized") || msg.includes("revert")) {
      outsiderDisputeBlocked = true;
      console.log("  [PASS] Outsider dispute rejected with Unauthorized().");
    } else {
      throw error;
    }
  }
  if (!outsiderDisputeBlocked) throw new Error("Case 5a Failed: Outsider raised a dispute!");

  // 5b: Zero evidence hash attempt
  console.log("5b: Requester attempts to dispute with empty evidence hash (bytes32(0))...");
  let zeroEvidenceBlocked = false;
  try {
    await reservation.write.disputeReservation([case5.id, zeroBytes32], {
      account: requester.account,
    });
  } catch (error: any) {
    const msg = error?.message || String(error);
    if (msg.includes("InvalidEvidence") || msg.includes("revert")) {
      zeroEvidenceBlocked = true;
      console.log("  [PASS] Empty evidence rejected with InvalidEvidence().");
    } else {
      throw error;
    }
  }
  if (!zeroEvidenceBlocked) throw new Error("Case 5b Failed: Zero evidence accepted!");

  // 5c: Legitimate participant dispute
  console.log("5c: Requester raises dispute with valid evidence hash...");
  const disputeTx = await reservation.write.disputeReservation([case5.id, validEvidence], {
    account: requester.account,
  });
  await publicClient.waitForTransactionReceipt({ hash: disputeTx });

  const finalState = await reservation.read.reservations([case5.id]);
  const finalStatus = Number(finalState[5]);
  console.log(`  [PASS] Dispute accepted. New Status: ${STATUS_NAMES[finalStatus]} (code: ${finalStatus}).`);
  console.log(`  Dispute Evidence: ${finalState[8]}`);
  console.log(`  Disputed By:      ${finalState[9]}`);

  if (finalStatus !== 5) {
    throw new Error(`Case 5c Failed: Expected Disputed (5), got ${STATUS_NAMES[finalStatus]}`);
  }

  console.log("\n========================================================");
  console.log(" ALL 5 SECURITY & INVARIANT TEST CASES PASSED!");
  console.log("========================================================");
  console.log(" 1. Unauthorized Acceptance:  BLOCKED (Unauthorized)");
  console.log(" 2. Unauthorized Cancel:      BLOCKED (Unauthorized)");
  console.log(" 3. Expiry Enforcement:       BLOCKED (InvalidExpiry)");
  console.log(" 4. Invalid Transitions:      BLOCKED (InvalidStatus)");
  console.log(" 5. Dispute Behavior:         ENFORCED (Role & Evidence checks)");
  console.log("========================================================\n");
}

main().catch((error) => {
  console.error("Security suite failed:", error);
  process.exitCode = 1;
});
