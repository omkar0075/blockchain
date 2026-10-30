import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { network } from "hardhat";
import { pad, parseEventLogs, stringToHex, zeroAddress } from "viem";

describe("MEDwayReservation", async function () {
  const { viem, networkHelpers } = await network.create();
  const publicClient = await viem.getPublicClient();
  const [deployer, requester, provider, thirdParty] = await viem.getWalletClients();

  const zeroBytes32 = "0x0000000000000000000000000000000000000000000000000000000000000000" as const;
  const unknownReservationId = "0x1111111111111111111111111111111111111111111111111111111111111111" as const;
  const sampleResourceRef = pad(stringToHex("diagnostic-mri-slot-101"), { size: 32 });
  const sampleEvidenceHash = pad(stringToHex("evidence-delayed-delivery"), { size: 32 });
  const sampleResolutionEvidence = pad(stringToHex("arbitration-resolution-memo"), { size: 32 });

  // Status enum mapping matching MEDwayReservation.sol
  const Status = {
    Proposed: 0,
    Committed: 1,
    Fulfilled: 2,
    Cancelled: 3,
    Expired: 4,
    Disputed: 5,
  } as const;

  async function deployFixture() {
    const medway = await viem.deployContract("MEDwayReservation");
    return { medway };
  }

  async function createSampleReservation(medwayContract: any, durationSeconds: bigint = 3600n) {
    const now = BigInt(await networkHelpers.time.latest());
    const expiresAt = now + durationSeconds;

    const hash = await medwayContract.write.createReservation(
      [provider.account.address, sampleResourceRef, expiresAt],
      { account: requester.account }
    );

    const receipt = await publicClient.waitForTransactionReceipt({ hash });
    const events = await publicClient.getContractEvents({
      address: medwayContract.address,
      abi: medwayContract.abi,
      eventName: "ReservationCreated",
      fromBlock: receipt.blockNumber,
      strict: true,
    });

    const reservationId = (events[0] as any).args.reservationId as `0x${string}`;
    return { reservationId, expiresAt };
  }

  describe("Deployment & Ownership", function () {
    it("Should set the deployer as owner", async function () {
      const { medway } = await deployFixture();
      const owner = await medway.read.owner();
      assert.equal(owner.toLowerCase(), deployer.account.address.toLowerCase());
    });

    it("Should define a 7-day dispute window", async function () {
      const { medway } = await deployFixture();
      const window = await medway.read.DISPUTE_WINDOW();
      assert.equal(window, 7n * 24n * 60n * 60n);
    });
  });

  // =========================================================================
  // Test 1: Create a reservation
  // =========================================================================
  describe("Test 1: Create a reservation", function () {
    it("Should create a reservation proposal and emit ReservationCreated with correct event args", async function () {
      const { medway } = await deployFixture();
      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 1800n;

      await viem.assertions.emit(
        medway.write.createReservation(
          [provider.account.address, sampleResourceRef, expiresAt],
          { account: requester.account }
        ),
        medway,
        "ReservationCreated"
      );
    });

    it("Should accurately store reservation details with status Proposed", async function () {
      const { medway } = await deployFixture();
      const { reservationId, expiresAt } = await createSampleReservation(medway);

      const exists = await medway.read.exists([reservationId]);
      assert.equal(exists, true);

      const res = await medway.read.reservations([reservationId]);
      // struct Reservation { id, resourceRef, requester, provider, expiresAt, status, createdAt, fulfilledAt, disputeEvidence, disputedBy }
      assert.equal(res[0], reservationId);
      assert.equal(res[1], sampleResourceRef);
      assert.equal(res[2].toLowerCase(), requester.account.address.toLowerCase());
      assert.equal(res[3].toLowerCase(), provider.account.address.toLowerCase());
      assert.equal(res[4], expiresAt);
      assert.equal(res[5], Status.Proposed);
      assert.equal(res[7], 0n); // fulfilledAt initially 0
      assert.equal(res[8], zeroBytes32); // disputeEvidence initially 0
      assert.equal(res[9].toLowerCase(), zeroAddress.toLowerCase()); // disputedBy initially 0
    });

    it("Should generate unique IDs for separate reservations", async function () {
      const { medway } = await deployFixture();
      const res1 = await createSampleReservation(medway);
      const res2 = await createSampleReservation(medway);
      assert.notEqual(res1.reservationId, res2.reservationId);
    });
  });

  // =========================================================================
  // Test 2: Check permissions
  // =========================================================================
  describe("Test 2: Check permissions", function () {
    it("Should allow the correct provider to accept the reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.emit(
        medway.write.acceptReservation([reservationId], { account: provider.account }),
        medway,
        "ReservationCommitted"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Committed);
    });

    it("Should reject an unrelated account from accepting the reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([reservationId], { account: thirdParty.account }),
        medway,
        "Unauthorized"
      );
    });

    it("Should reject the requester from accepting their own reservation proposal", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([reservationId], { account: requester.account }),
        medway,
        "Unauthorized"
      );
    });
  });

  // =========================================================================
  // Test 3: Check the reservation lifecycle
  // =========================================================================
  describe("Test 3: Check the reservation lifecycle", function () {
    it("Should complete the full lifecycle: Proposed -> Committed -> Fulfilled", async function () {
      const { medway } = await deployFixture();

      // Step 1: Requester creates proposal -> Status: Proposed
      const { reservationId } = await createSampleReservation(medway);
      let res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Proposed);

      // Step 2: Provider accepts -> Status: Committed
      await viem.assertions.emit(
        medway.write.acceptReservation([reservationId], { account: provider.account }),
        medway,
        "ReservationCommitted"
      );
      res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Committed);

      // Step 3: Provider fulfills -> Status: Fulfilled
      await viem.assertions.emit(
        medway.write.fulfillReservation([reservationId], { account: provider.account }),
        medway,
        "ReservationFulfilled"
      );
      res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Fulfilled);
      assert.ok(res[7] > 0n, "fulfilledAt should be set");
    });
  });

  // =========================================================================
  // Test 4: Check expiry rules
  // =========================================================================
  describe("Test 4: Check expiry", function () {
    it("Should reject acceptReservation after the expiry deadline has passed", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 100n);

      await networkHelpers.time.increase(150n);

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidExpiry"
      );
    });

    it("Should reject fulfillReservation after the expiry deadline has passed", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 100n);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      // Fast forward past expiration
      await networkHelpers.time.increase(150n);

      // Late fulfillment must be blocked
      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidExpiry"
      );
    });

    it("Should reject cancelReservation after expiry deadline has passed", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 100n);

      await networkHelpers.time.increase(150n);

      await viem.assertions.revertWithCustomError(
        medway.write.cancelReservation([reservationId], { account: requester.account }),
        medway,
        "InvalidExpiry"
      );
    });

    it("Should allow anyone to mark an uncompleted reservation Expired past its deadline", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 50n);

      await networkHelpers.time.increase(100n);

      await viem.assertions.emit(
        medway.write.expireReservation([reservationId], { account: thirdParty.account }),
        medway,
        "ReservationExpired"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Expired);
    });

    it("Should revert with NotExpired if attempting to expire before deadline", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 3600n);

      await viem.assertions.revertWithCustomError(
        medway.write.expireReservation([reservationId], { account: thirdParty.account }),
        medway,
        "NotExpired"
      );
    });

    it("Should ensure an expired reservation cannot be accepted", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 60n);

      await networkHelpers.time.increase(100n);
      await medway.write.expireReservation([reservationId], { account: thirdParty.account });

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );
    });

    it("Should ensure an expired reservation cannot be fulfilled", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 60n);

      await networkHelpers.time.increase(100n);
      await medway.write.expireReservation([reservationId], { account: thirdParty.account });

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );
    });
  });

  // =========================================================================
  // Test 5: Try invalid actions & edge cases
  // =========================================================================
  describe("Test 5: Try invalid actions", function () {
    it("Should reject accepting the same reservation twice", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );
    });

    it("Should reject fulfilling a reservation before it is committed", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );
    });

    it("Should revert with ReservationNotFound when using an unknown reservation ID", async function () {
      const { medway } = await deployFixture();

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([unknownReservationId], { account: provider.account }),
        medway,
        "ReservationNotFound"
      );

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([unknownReservationId], { account: provider.account }),
        medway,
        "ReservationNotFound"
      );

      await viem.assertions.revertWithCustomError(
        medway.write.cancelReservation([unknownReservationId], { account: requester.account }),
        medway,
        "ReservationNotFound"
      );

      await viem.assertions.revertWithCustomError(
        medway.write.expireReservation([unknownReservationId], { account: thirdParty.account }),
        medway,
        "ReservationNotFound"
      );

      await viem.assertions.revertWithCustomError(
        medway.write.disputeReservation([unknownReservationId, sampleEvidenceHash], {
          account: requester.account,
        }),
        medway,
        "ReservationNotFound"
      );
    });

    it("Should revert with InvalidAddress if provider is address(0)", async function () {
      const { medway } = await deployFixture();
      const now = BigInt(await networkHelpers.time.latest());

      await viem.assertions.revertWithCustomError(
        medway.write.createReservation(
          [zeroAddress, sampleResourceRef, now + 1000n],
          { account: requester.account }
        ),
        medway,
        "InvalidAddress"
      );
    });

    it("Should revert with InvalidAddress if requester sets self as provider", async function () {
      const { medway } = await deployFixture();
      const now = BigInt(await networkHelpers.time.latest());

      await viem.assertions.revertWithCustomError(
        medway.write.createReservation(
          [requester.account.address, sampleResourceRef, now + 1000n],
          { account: requester.account }
        ),
        medway,
        "InvalidAddress"
      );
    });

    it("Should revert with InvalidExpiry if expiresAt is now or in the past", async function () {
      const { medway } = await deployFixture();
      const now = BigInt(await networkHelpers.time.latest());

      await viem.assertions.revertWithCustomError(
        medway.write.createReservation(
          [provider.account.address, sampleResourceRef, now - 10n],
          { account: requester.account }
        ),
        medway,
        "InvalidExpiry"
      );
    });

    it("Should revert with InvalidEvidence if resourceRef is bytes32(0)", async function () {
      const { medway } = await deployFixture();
      const now = BigInt(await networkHelpers.time.latest());

      await viem.assertions.revertWithCustomError(
        medway.write.createReservation(
          [provider.account.address, zeroBytes32, now + 1000n],
          { account: requester.account }
        ),
        medway,
        "InvalidEvidence"
      );
    });

    it("Should revert with Unauthorized if non-provider tries to fulfill", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([reservationId], { account: thirdParty.account }),
        medway,
        "Unauthorized"
      );
    });

    it("Should revert with InvalidStatus if fulfilling an already fulfilled reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });
      await medway.write.fulfillReservation([reservationId], { account: provider.account });

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );
    });

    it("Should revert with InvalidStatus if fulfilling a cancelled reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.cancelReservation([reservationId], { account: requester.account });

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );
    });
  });

  // =========================================================================
  // Test 6: Cancellation and disputes
  // =========================================================================
  describe("Test 6: Cancellation and disputes", function () {
    it("Should allow requester to cancel a proposed reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.emit(
        medway.write.cancelReservation([reservationId], { account: requester.account }),
        medway,
        "ReservationCancelled"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Cancelled);
    });

    it("Should allow provider to cancel a committed reservation before expiry", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      await viem.assertions.emit(
        medway.write.cancelReservation([reservationId], { account: provider.account }),
        medway,
        "ReservationCancelled"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Cancelled);
    });

    it("Should allow requester to cancel a committed reservation before expiry", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      await viem.assertions.emit(
        medway.write.cancelReservation([reservationId], { account: requester.account }),
        medway,
        "ReservationCancelled"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Cancelled);
    });

    it("Should revert with Unauthorized if third party attempts to cancel", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.cancelReservation([reservationId], { account: thirdParty.account }),
        medway,
        "Unauthorized"
      );
    });

    it("Should revert with InvalidStatus if canceling an already fulfilled reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });
      await medway.write.fulfillReservation([reservationId], { account: provider.account });

      await viem.assertions.revertWithCustomError(
        medway.write.cancelReservation([reservationId], { account: requester.account }),
        medway,
        "InvalidStatus"
      );
    });

    it("Should allow requester to dispute a committed reservation with evidence", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      await viem.assertions.emit(
        medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
          account: requester.account,
        }),
        medway,
        "ReservationDisputed"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Disputed);
      assert.equal(res[8], sampleEvidenceHash);
      assert.equal(res[9].toLowerCase(), requester.account.address.toLowerCase());
    });

    it("Should allow provider to dispute a fulfilled reservation within the dispute window", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });
      await medway.write.fulfillReservation([reservationId], { account: provider.account });

      // Fast forward 2 days (within 7 day window)
      await networkHelpers.time.increase(2n * 24n * 3600n);

      await viem.assertions.emit(
        medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
          account: provider.account,
        }),
        medway,
        "ReservationDisputed"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Disputed);
      assert.equal(res[8], sampleEvidenceHash);
      assert.equal(res[9].toLowerCase(), provider.account.address.toLowerCase());
    });

    it("Should revert with DisputeWindowClosed if disputing a fulfilled reservation after 7 days", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });
      await medway.write.fulfillReservation([reservationId], { account: provider.account });

      // Advance time beyond 7 days
      await networkHelpers.time.increase(8n * 24n * 3600n);

      await viem.assertions.revertWithCustomError(
        medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
          account: requester.account,
        }),
        medway,
        "DisputeWindowClosed"
      );
    });

    it("Should revert with InvalidStatus if disputing a proposed reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
          account: requester.account,
        }),
        medway,
        "InvalidStatus"
      );
    });

    it("Should revert with InvalidEvidence if evidence hash is bytes32(0)", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      await viem.assertions.revertWithCustomError(
        medway.write.disputeReservation([reservationId, zeroBytes32], {
          account: requester.account,
        }),
        medway,
        "InvalidEvidence"
      );
    });

    it("Should revert with Unauthorized if third party attempts to dispute", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await medway.write.acceptReservation([reservationId], { account: provider.account });

      await viem.assertions.revertWithCustomError(
        medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
          account: thirdParty.account,
        }),
        medway,
        "Unauthorized"
      );
    });
  });

  // =========================================================================
  // Test 7: Dispute Resolution
  // =========================================================================
  describe("Test 7: Dispute Resolution", function () {
    async function createDisputedReservation(medway: any) {
      const { reservationId } = await createSampleReservation(medway);
      await medway.write.acceptReservation([reservationId], { account: provider.account });
      await medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
        account: requester.account,
      });
      return { reservationId };
    }

    it("Should allow the owner/arbitrator to resolve a dispute to Fulfilled", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createDisputedReservation(medway);

      await viem.assertions.emit(
        medway.write.resolveDispute(
          [reservationId, Status.Fulfilled, sampleResolutionEvidence],
          { account: deployer.account }
        ),
        medway,
        "ReservationDisputeResolved"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Fulfilled);
    });

    it("Should allow the owner/arbitrator to resolve a dispute to Cancelled", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createDisputedReservation(medway);

      await viem.assertions.emit(
        medway.write.resolveDispute(
          [reservationId, Status.Cancelled, sampleResolutionEvidence],
          { account: deployer.account }
        ),
        medway,
        "ReservationDisputeResolved"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Cancelled);
    });

    it("Should revert with Unauthorized if non-owner attempts to resolve a dispute", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createDisputedReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.resolveDispute(
          [reservationId, Status.Cancelled, sampleResolutionEvidence],
          { account: thirdParty.account }
        ),
        medway,
        "Unauthorized"
      );
    });

    it("Should revert with InvalidStatus if attempting to resolve a non-disputed reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.resolveDispute(
          [reservationId, Status.Cancelled, sampleResolutionEvidence],
          { account: deployer.account }
        ),
        medway,
        "InvalidStatus"
      );
    });

    it("Should revert with InvalidStatus if resolving to an invalid outcome status", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createDisputedReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.resolveDispute(
          [reservationId, Status.Proposed, sampleResolutionEvidence],
          { account: deployer.account }
        ),
        medway,
        "InvalidStatus"
      );
    });

    it("Should revert with InvalidEvidence if resolution evidence hash is bytes32(0)", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createDisputedReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.resolveDispute(
          [reservationId, Status.Cancelled, zeroBytes32],
          { account: deployer.account }
        ),
        medway,
        "InvalidEvidence"
      );
    });
  });

  // =========================================================================
  // Milestone: Contract Security Checks (Pre-Deployment Validation)
  // =========================================================================
  describe("Milestone: Contract Security Checks", function () {
    it("1. Unauthorized acceptance: an outsider cannot accept a reservation on behalf of the provider", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([reservationId], { account: thirdParty.account }),
        medway,
        "Unauthorized"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Proposed);
    });

    it("2. Unauthorized cancellation: an unrelated account cannot cancel a reservation", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);

      await viem.assertions.revertWithCustomError(
        medway.write.cancelReservation([reservationId], { account: thirdParty.account }),
        medway,
        "Unauthorized"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Proposed);
    });

    it("3. Expiry enforcement: an expired reservation cannot be accepted", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway, 100n);

      await networkHelpers.time.increase(150n);

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservation([reservationId], { account: provider.account }),
        medway,
        "InvalidExpiry"
      );
    });

    it("4. Invalid state transitions: a fulfilled or cancelled reservation cannot be fulfilled again", async function () {
      const { medway } = await deployFixture();

      // 4a: A fulfilled reservation cannot be fulfilled again
      const { reservationId: fulfilledId } = await createSampleReservation(medway);
      await medway.write.acceptReservation([fulfilledId], { account: provider.account });
      await medway.write.fulfillReservation([fulfilledId], { account: provider.account });

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([fulfilledId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );

      // 4b: A cancelled reservation cannot be fulfilled
      const { reservationId: cancelledId } = await createSampleReservation(medway);
      await medway.write.cancelReservation([cancelledId], { account: requester.account });

      await viem.assertions.revertWithCustomError(
        medway.write.fulfillReservation([cancelledId], { account: provider.account }),
        medway,
        "InvalidStatus"
      );
    });

    it("5. Dispute behavior: only permitted participants can raise a dispute, and invalid evidence is rejected", async function () {
      const { medway } = await deployFixture();
      const { reservationId } = await createSampleReservation(medway);
      await medway.write.acceptReservation([reservationId], { account: provider.account });

      // Outsider attempt rejected
      await viem.assertions.revertWithCustomError(
        medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
          account: thirdParty.account,
        }),
        medway,
        "Unauthorized"
      );

      // Zero evidence hash rejected
      await viem.assertions.revertWithCustomError(
        medway.write.disputeReservation([reservationId, zeroBytes32], {
          account: requester.account,
        }),
        medway,
        "InvalidEvidence"
      );

      // Legitimate participant with valid evidence accepted
      await viem.assertions.emit(
        medway.write.disputeReservation([reservationId, sampleEvidenceHash], {
          account: requester.account,
        }),
        medway,
        "ReservationDisputed"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Disputed);
      assert.equal(res[8], sampleEvidenceHash);
      assert.equal(res[9].toLowerCase(), requester.account.address.toLowerCase());
    });
  });

  // =========================================================================
  // Milestone: EIP-712 Gasless Meta-Transactions
  // =========================================================================
  describe("EIP-712 Gasless Meta-Transactions", function () {
    async function getEip712Domain(medwayAddress: `0x${string}`) {
      const chainId = await publicClient.getChainId();
      return {
        name: "MEDwayReservation",
        version: "1",
        chainId: Number(chainId),
        verifyingContract: medwayAddress,
      };
    }

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

    const acceptRequestTypes = {
      AcceptRequest: [
        { name: "reservationId", type: "bytes32" },
        { name: "provider", type: "address" },
        { name: "nonce", type: "uint256" },
        { name: "deadline", type: "uint256" },
      ],
    } as const;

    it("Should allow a relayer to submit createReservationWithSig recording the hospital as requester", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 3600n;
      const deadline = now + 1800n;
      const nonce = await medway.read.nonces([requester.account.address]);

      // Hospital A (requester) signs off-chain typed data
      const signature = await requester.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      // Third party (relayer/backend) broadcasts transaction and pays gas
      const txHash = await medway.write.createReservationWithSig(
        [
          requester.account.address,
          provider.account.address,
          sampleResourceRef,
          expiresAt,
          deadline,
          signature,
        ],
        { account: thirdParty.account }
      );

      const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
      const events = await publicClient.getContractEvents({
        address: medway.address,
        abi: medway.abi,
        eventName: "ReservationCreated",
        fromBlock: receipt.blockNumber,
        strict: true,
      });

      const reservationId = (events[0] as any).args.reservationId as `0x${string}`;

      // CRITICAL CHECK: The contract must record requester as the hospital, NOT thirdParty (relayer)
      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[2].toLowerCase(), requester.account.address.toLowerCase());
      assert.notEqual(res[2].toLowerCase(), thirdParty.account.address.toLowerCase());
      assert.equal(res[3].toLowerCase(), provider.account.address.toLowerCase());
      assert.equal(res[5], Status.Proposed);

      // Nonce must have incremented
      const nextNonce = await medway.read.nonces([requester.account.address]);
      assert.equal(nextNonce, nonce + 1n);
    });

    it("Should reject createReservationWithSig if the signature is invalid or forged", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 3600n;
      const deadline = now + 1800n;
      const nonce = await medway.read.nonces([requester.account.address]);

      // Forged: Third-party signs claiming to be requester
      const badSignature = await thirdParty.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      await viem.assertions.revertWithCustomError(
        medway.write.createReservationWithSig(
          [
            requester.account.address,
            provider.account.address,
            sampleResourceRef,
            expiresAt,
            deadline,
            badSignature,
          ],
          { account: thirdParty.account }
        ),
        medway,
        "InvalidSignature"
      );
    });

    it("Should reject createReservationWithSig if the deadline has passed", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 3600n;
      const deadline = now + 50n;
      const nonce = await medway.read.nonces([requester.account.address]);

      const signature = await requester.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      // Advance time past signature deadline
      await networkHelpers.time.increase(100n);

      await viem.assertions.revertWithCustomError(
        medway.write.createReservationWithSig(
          [
            requester.account.address,
            provider.account.address,
            sampleResourceRef,
            expiresAt,
            deadline,
            signature,
          ],
          { account: thirdParty.account }
        ),
        medway,
        "SignatureExpired"
      );
    });

    it("Should prevent signature replay attacks using nonces", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 3600n;
      const deadline = now + 1800n;
      const nonce = await medway.read.nonces([requester.account.address]);

      const signature = await requester.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      // First submission succeeds
      await medway.write.createReservationWithSig(
        [
          requester.account.address,
          provider.account.address,
          sampleResourceRef,
          expiresAt,
          deadline,
          signature,
        ],
        { account: thirdParty.account }
      );

      // Replay attack: submitting the exact same signature must revert
      await viem.assertions.revertWithCustomError(
        medway.write.createReservationWithSig(
          [
            requester.account.address,
            provider.account.address,
            sampleResourceRef,
            expiresAt,
            deadline,
            signature,
          ],
          { account: thirdParty.account }
        ),
        medway,
        "InvalidSignature"
      );
    });

    it("Should allow relayer to submit acceptReservationWithSig on behalf of provider", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      // Create initial proposal directly
      const { reservationId } = await createSampleReservation(medway);

      const now = BigInt(await networkHelpers.time.latest());
      const deadline = now + 1800n;
      const providerNonce = await medway.read.nonces([provider.account.address]);

      // Provider signs off-chain
      const acceptSig = await provider.signTypedData({
        domain,
        types: acceptRequestTypes,
        primaryType: "AcceptRequest",
        message: {
          reservationId,
          provider: provider.account.address,
          nonce: providerNonce,
          deadline,
        },
      });

      // Relayer broadcasts acceptance
      await viem.assertions.emit(
        medway.write.acceptReservationWithSig(
          [reservationId, deadline, acceptSig],
          { account: thirdParty.account }
        ),
        medway,
        "ReservationCommitted"
      );

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Committed);
    });

    it("Should decode receipt logs directly using parseEventLogs and verify event arguments", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 7200n;
      const deadline = now + 1800n;
      const nonce = await medway.read.nonces([requester.account.address]);

      const createSig = await requester.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      // Submit via relayer
      const createTxHash = await medway.write.createReservationWithSig(
        [
          requester.account.address,
          provider.account.address,
          sampleResourceRef,
          expiresAt,
          deadline,
          createSig,
        ],
        { account: thirdParty.account }
      );

      const createReceipt = await publicClient.waitForTransactionReceipt({ hash: createTxHash });
      const createdLogs = parseEventLogs({
        abi: medway.abi,
        logs: createReceipt.logs,
        eventName: "ReservationCreated",
      });

      assert.equal(createdLogs.length, 1);
      const createdEvent = createdLogs[0];
      const reservationId = createdEvent.args.reservationId;

      assert.equal(createdEvent.args.requester.toLowerCase(), requester.account.address.toLowerCase());
      assert.equal(createdEvent.args.provider.toLowerCase(), provider.account.address.toLowerCase());
      assert.equal(createdEvent.args.resourceRef, sampleResourceRef);
      assert.equal(createdEvent.args.expiresAt, expiresAt);

      // Now Provider accepts via relayer
      const providerNonce = await medway.read.nonces([provider.account.address]);
      const acceptDeadline = now + 1800n;
      const acceptSig = await provider.signTypedData({
        domain,
        types: acceptRequestTypes,
        primaryType: "AcceptRequest",
        message: {
          reservationId,
          provider: provider.account.address,
          nonce: providerNonce,
          deadline: acceptDeadline,
        },
      });

      const acceptTxHash = await medway.write.acceptReservationWithSig(
        [reservationId, acceptDeadline, acceptSig],
        { account: thirdParty.account }
      );

      const acceptReceipt = await publicClient.waitForTransactionReceipt({ hash: acceptTxHash });
      const acceptLogs = parseEventLogs({
        abi: medway.abi,
        logs: acceptReceipt.logs,
        eventName: "ReservationCommitted",
      });

      assert.equal(acceptLogs.length, 1);
      assert.equal(acceptLogs[0].args.reservationId, reservationId);

      const onChain = await medway.read.reservations([reservationId]);
      assert.equal(onChain[5], Status.Committed);
    });

    it("Should allow requester to cancel a gasless proposal before expiry", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 7200n;
      const deadline = now + 1800n;
      const nonce = await medway.read.nonces([requester.account.address]);

      const createSig = await requester.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      const createTxHash = await medway.write.createReservationWithSig(
        [
          requester.account.address,
          provider.account.address,
          sampleResourceRef,
          expiresAt,
          deadline,
          createSig,
        ],
        { account: thirdParty.account }
      );

      const receipt = await publicClient.waitForTransactionReceipt({ hash: createTxHash });
      const createdLogs = parseEventLogs({
        abi: medway.abi,
        logs: receipt.logs,
        eventName: "ReservationCreated",
      });
      const reservationId = createdLogs[0].args.reservationId;

      // Requester cancels proposal
      const cancelTxHash = await medway.write.cancelReservation([reservationId], {
        account: requester.account,
      });

      const cancelReceipt = await publicClient.waitForTransactionReceipt({ hash: cancelTxHash });
      const cancelLogs = parseEventLogs({
        abi: medway.abi,
        logs: cancelReceipt.logs,
        eventName: "ReservationCancelled",
      });

      assert.equal(cancelLogs.length, 1);
      assert.equal(cancelLogs[0].args.reservationId, reservationId);
      assert.equal(cancelLogs[0].args.actor.toLowerCase(), requester.account.address.toLowerCase());

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Cancelled);
    });

    it("Should transition an unaccepted gasless proposal to Expired after deadline passes", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 100n;
      const deadline = now + 1800n;
      const nonce = await medway.read.nonces([requester.account.address]);

      const createSig = await requester.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      const createTxHash = await medway.write.createReservationWithSig(
        [
          requester.account.address,
          provider.account.address,
          sampleResourceRef,
          expiresAt,
          deadline,
          createSig,
        ],
        { account: thirdParty.account }
      );

      const receipt = await publicClient.waitForTransactionReceipt({ hash: createTxHash });
      const createdLogs = parseEventLogs({
        abi: medway.abi,
        logs: receipt.logs,
        eventName: "ReservationCreated",
      });
      const reservationId = createdLogs[0].args.reservationId;

      // Advance time past expiresAt
      await networkHelpers.time.increase(101n);

      // Third party calls expireReservation
      const expireTxHash = await medway.write.expireReservation([reservationId], {
        account: thirdParty.account,
      });

      const expireReceipt = await publicClient.waitForTransactionReceipt({ hash: expireTxHash });
      const expireLogs = parseEventLogs({
        abi: medway.abi,
        logs: expireReceipt.logs,
        eventName: "ReservationExpired",
      });

      assert.equal(expireLogs.length, 1);
      assert.equal(expireLogs[0].args.reservationId, reservationId);

      const res = await medway.read.reservations([reservationId]);
      assert.equal(res[5], Status.Expired);
    });

    it("Should reject acceptReservationWithSig if the reservation has already expired", async function () {
      const { medway } = await deployFixture();
      const domain = await getEip712Domain(medway.address);

      const now = BigInt(await networkHelpers.time.latest());
      const expiresAt = now + 100n;
      const deadline = now + 1800n;
      const nonce = await medway.read.nonces([requester.account.address]);

      const createSig = await requester.signTypedData({
        domain,
        types: reservationRequestTypes,
        primaryType: "ReservationRequest",
        message: {
          requester: requester.account.address,
          provider: provider.account.address,
          resourceRef: sampleResourceRef,
          expiresAt,
          nonce,
          deadline,
        },
      });

      const createTxHash = await medway.write.createReservationWithSig(
        [
          requester.account.address,
          provider.account.address,
          sampleResourceRef,
          expiresAt,
          deadline,
          createSig,
        ],
        { account: thirdParty.account }
      );

      const receipt = await publicClient.waitForTransactionReceipt({ hash: createTxHash });
      const createdLogs = parseEventLogs({
        abi: medway.abi,
        logs: receipt.logs,
        eventName: "ReservationCreated",
      });
      const reservationId = createdLogs[0].args.reservationId;

      // Advance time past expiresAt (reservation expiry)
      await networkHelpers.time.increase(101n);

      const providerNonce = await medway.read.nonces([provider.account.address]);
      const acceptDeadline = now + 1800n; // signature deadline is still valid, but reservation expiresAt has passed
      const acceptSig = await provider.signTypedData({
        domain,
        types: acceptRequestTypes,
        primaryType: "AcceptRequest",
        message: {
          reservationId,
          provider: provider.account.address,
          nonce: providerNonce,
          deadline: acceptDeadline,
        },
      });

      await viem.assertions.revertWithCustomError(
        medway.write.acceptReservationWithSig(
          [reservationId, acceptDeadline, acceptSig],
          { account: thirdParty.account }
        ),
        medway,
        "InvalidExpiry"
      );
    });
  });
});
