// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/// @title MEDwayReservation
/// @notice On-chain commitments for MEDway healthcare resource coordination.
/// @dev Patient data, inventory details, and private negotiations stay off-chain.
contract MEDwayReservation {
    enum Status {
        Proposed,
        Committed,
        Fulfilled,
        Cancelled,
        Expired,
        Disputed
    }

    struct Reservation {
        bytes32 id;
        bytes32 resourceRef;
        address requester;
        address provider;
        uint64 expiresAt;
        Status status;
        uint64 createdAt;
        uint64 fulfilledAt;
        bytes32 disputeEvidence;
        address disputedBy;
    }

    /// @notice Standard dispute window permitted after a reservation is fulfilled (7 days).
    uint64 public constant DISPUTE_WINDOW = 7 days;

    address public immutable owner;
    uint256 private nextNonce = 1;

    mapping(bytes32 => Reservation) public reservations;
    mapping(bytes32 => bool) public exists;

    event ReservationCreated(
        bytes32 indexed reservationId,
        address indexed requester,
        address indexed provider,
        bytes32 resourceRef,
        uint64 expiresAt
    );

    event ReservationCommitted(bytes32 indexed reservationId);
    event ReservationFulfilled(bytes32 indexed reservationId, uint64 fulfilledAt);
    event ReservationCancelled(bytes32 indexed reservationId, address indexed actor);
    event ReservationExpired(bytes32 indexed reservationId);
    event ReservationDisputed(
        bytes32 indexed reservationId,
        address indexed actor,
        bytes32 evidenceHash
    );
    event ReservationDisputeResolved(
        bytes32 indexed reservationId,
        address indexed resolver,
        Status resolutionStatus,
        bytes32 resolutionEvidence
    );

    error Unauthorized();
    error InvalidAddress();
    error InvalidExpiry();
    error ReservationNotFound();
    error InvalidStatus();
    error NotExpired();
    error InvalidEvidence();
    error DisputeWindowClosed();

    constructor() {
        owner = msg.sender;
    }

    /// @notice Create a proposal. The requester authorizes it by submitting this transaction.
    /// @param provider The institution expected to provide the resource.
    /// @param resourceRef A hash/reference to off-chain reservation details.
    /// @param expiresAt Unix timestamp after which the proposal expires.
    function createReservation(
        address provider,
        bytes32 resourceRef,
        uint64 expiresAt
    ) external returns (bytes32 reservationId) {
        if (provider == address(0) || provider == msg.sender) {
            revert InvalidAddress();
        }
        if (expiresAt <= block.timestamp) revert InvalidExpiry();
        if (resourceRef == bytes32(0)) revert InvalidEvidence();

        reservationId = keccak256(
            abi.encode(
                block.chainid,
                address(this),
                msg.sender,
                provider,
                resourceRef,
                nextNonce++
            )
        );

        exists[reservationId] = true;
        reservations[reservationId] = Reservation({
            id: reservationId,
            resourceRef: resourceRef,
            requester: msg.sender,
            provider: provider,
            expiresAt: expiresAt,
            status: Status.Proposed,
            createdAt: uint64(block.timestamp),
            fulfilledAt: 0,
            disputeEvidence: bytes32(0),
            disputedBy: address(0)
        });

        emit ReservationCreated(
            reservationId,
            msg.sender,
            provider,
            resourceRef,
            expiresAt
        );
    }

    /// @notice Provider accepts the request, creating an on-chain commitment.
    function acceptReservation(bytes32 reservationId) external {
        Reservation storage r = _get(reservationId);
        if (msg.sender != r.provider) revert Unauthorized();
        if (r.status != Status.Proposed) revert InvalidStatus();
        if (block.timestamp >= r.expiresAt) revert InvalidExpiry();

        r.status = Status.Committed;
        emit ReservationCommitted(reservationId);
    }

    /// @notice Provider attests that the off-chain resource was delivered.
    /// @dev Cannot fulfill after the agreed expiration deadline has passed.
    function fulfillReservation(bytes32 reservationId) external {
        Reservation storage r = _get(reservationId);
        if (msg.sender != r.provider) revert Unauthorized();
        if (r.status != Status.Committed) revert InvalidStatus();
        if (block.timestamp > r.expiresAt) revert InvalidExpiry();

        r.status = Status.Fulfilled;
        r.fulfilledAt = uint64(block.timestamp);
        emit ReservationFulfilled(reservationId, r.fulfilledAt);
    }

    /// @notice Requester or provider can cancel a proposal or commitment before expiry.
    function cancelReservation(bytes32 reservationId) external {
        Reservation storage r = _get(reservationId);
        if (msg.sender != r.requester && msg.sender != r.provider) {
            revert Unauthorized();
        }
        if (
            r.status != Status.Proposed &&
            r.status != Status.Committed
        ) revert InvalidStatus();
        if (block.timestamp >= r.expiresAt) revert InvalidExpiry();

        r.status = Status.Cancelled;
        emit ReservationCancelled(reservationId, msg.sender);
    }

    /// @notice Anyone can mark an uncompleted reservation expired after its deadline.
    function expireReservation(bytes32 reservationId) external {
        Reservation storage r = _get(reservationId);
        if (
            r.status != Status.Proposed &&
            r.status != Status.Committed
        ) revert InvalidStatus();
        if (block.timestamp < r.expiresAt) revert NotExpired();

        r.status = Status.Expired;
        emit ReservationExpired(reservationId);
    }

    /// @notice Either party can raise a dispute using a non-sensitive evidence hash.
    /// @dev If already fulfilled, dispute must be initiated within DISPUTE_WINDOW.
    function disputeReservation(
        bytes32 reservationId,
        bytes32 evidenceHash
    ) external {
        Reservation storage r = _get(reservationId);
        if (msg.sender != r.requester && msg.sender != r.provider) {
            revert Unauthorized();
        }
        if (
            r.status != Status.Committed &&
            r.status != Status.Fulfilled
        ) revert InvalidStatus();
        if (evidenceHash == bytes32(0)) revert InvalidEvidence();

        if (r.status == Status.Fulfilled) {
            if (block.timestamp > r.fulfilledAt + DISPUTE_WINDOW) {
                revert DisputeWindowClosed();
            }
        }

        r.status = Status.Disputed;
        r.disputeEvidence = evidenceHash;
        r.disputedBy = msg.sender;
        emit ReservationDisputed(reservationId, msg.sender, evidenceHash);
    }

    /// @notice Authorized consortium owner/arbitrator resolves an active dispute.
    /// @param reservationId The reservation under dispute.
    /// @param resolution Final resolved status (must be Fulfilled or Cancelled).
    /// @param resolutionEvidence Hash referencing arbitration findings.
    function resolveDispute(
        bytes32 reservationId,
        Status resolution,
        bytes32 resolutionEvidence
    ) external {
        if (msg.sender != owner) revert Unauthorized();
        Reservation storage r = _get(reservationId);
        if (r.status != Status.Disputed) revert InvalidStatus();
        if (
            resolution != Status.Fulfilled &&
            resolution != Status.Cancelled
        ) revert InvalidStatus();
        if (resolutionEvidence == bytes32(0)) revert InvalidEvidence();

        r.status = resolution;
        emit ReservationDisputeResolved(
            reservationId,
            msg.sender,
            resolution,
            resolutionEvidence
        );
    }

    function _get(
        bytes32 reservationId
    ) private view returns (Reservation storage r) {
        if (!exists[reservationId]) revert ReservationNotFound();
        return reservations[reservationId];
    }
}
