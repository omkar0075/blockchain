import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { network } from "hardhat";
import { pad, stringToHex, zeroAddress } from "viem";

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
});
