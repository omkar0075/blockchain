// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/// @title MEDwayReservation
/// @notice On-chain commitments for MEDway healthcare resource coordination with EIP-712 meta-transaction support.
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

    // EIP-712 TypeHashes
    bytes32 public constant EIP712_DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );
    bytes32 public constant NAME_HASH = keccak256(bytes("MEDwayReservation"));
    bytes32 public constant VERSION_HASH = keccak256(bytes("1"));

    bytes32 public constant RESERVATION_REQUEST_TYPEHASH = keccak256(
        "ReservationRequest(address requester,address provider,bytes32 resourceRef,uint64 expiresAt,uint256 nonce,uint256 deadline)"
    );

    bytes32 public constant ACCEPT_REQUEST_TYPEHASH = keccak256(
        "AcceptRequest(bytes32 reservationId,address provider,uint256 nonce,uint256 deadline)"
    );

    address public immutable owner;
    uint256 private nextNonce = 1;

    // EIP-712 Cached Domain Separator variables
    bytes32 private immutable _CACHED_DOMAIN_SEPARATOR;
    uint256 private immutable _CACHED_CHAIN_ID;
    address private immutable _CACHED_THIS;

    /// @notice Replay protection nonces per institution address
    mapping(address => uint256) public nonces;

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
    error InvalidSignature();
    error SignatureExpired();

    constructor() {
        owner = msg.sender;
        _CACHED_CHAIN_ID = block.chainid;
        _CACHED_THIS = address(this);
        _CACHED_DOMAIN_SEPARATOR = _buildDomainSeparator();
    }

    function _buildDomainSeparator() private view returns (bytes32) {
        return keccak256(
            abi.encode(
                EIP712_DOMAIN_TYPEHASH,
                NAME_HASH,
                VERSION_HASH,
                block.chainid,
                address(this)
            )
        );
    }

    /// @notice EIP-712 Domain Separator for off-chain typed signature validation.
    function DOMAIN_SEPARATOR() public view returns (bytes32) {
        if (block.chainid == _CACHED_CHAIN_ID && address(this) == _CACHED_THIS) {
            return _CACHED_DOMAIN_SEPARATOR;
        }
        return _buildDomainSeparator();
    }

    /// @notice Direct reservation proposal by requester.
    function createReservation(
        address provider,
        bytes32 resourceRef,
        uint64 expiresAt
    ) external returns (bytes32 reservationId) {
        return _createReservationInternal(msg.sender, provider, resourceRef, expiresAt);
    }

    /// @notice Gas-sponsored reservation proposal via EIP-712 signature verification.
    /// @dev Relayer calls this and pays gas; requester's identity is verified from signature.
    function createReservationWithSig(
        address requester,
        address provider,
        bytes32 resourceRef,
        uint64 expiresAt,
        uint256 deadline,
        bytes calldata signature
    ) external returns (bytes32 reservationId) {
        if (block.timestamp > deadline) revert SignatureExpired();
        if (requester == address(0)) revert InvalidAddress();

        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                DOMAIN_SEPARATOR(),
                keccak256(
                    abi.encode(
                        RESERVATION_REQUEST_TYPEHASH,
                        requester,
                        provider,
                        resourceRef,
                        expiresAt,
                        nonces[requester]++,
                        deadline
                    )
                )
            )
        );

        address recovered = _recoverSigner(digest, signature);
        if (recovered == address(0) || recovered != requester) {
            revert InvalidSignature();
        }

        return _createReservationInternal(requester, provider, resourceRef, expiresAt);
    }

    function _createReservationInternal(
        address requester,
        address provider,
        bytes32 resourceRef,
        uint64 expiresAt
    ) private returns (bytes32 reservationId) {
        if (provider == address(0) || provider == requester) {
            revert InvalidAddress();
        }
        if (expiresAt <= block.timestamp) revert InvalidExpiry();
        if (resourceRef == bytes32(0)) revert InvalidEvidence();

        reservationId = keccak256(
            abi.encode(
                block.chainid,
                address(this),
                requester,
                provider,
                resourceRef,
                nextNonce++
            )
        );

        exists[reservationId] = true;
        reservations[reservationId] = Reservation({
            id: reservationId,
            resourceRef: resourceRef,
            requester: requester,
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
            requester,
            provider,
            resourceRef,
            expiresAt
        );
    }

    /// @notice Provider accepts the request directly, creating an on-chain commitment.
    function acceptReservation(bytes32 reservationId) external {
        Reservation storage r = _get(reservationId);
        if (msg.sender != r.provider) revert Unauthorized();
        _acceptReservationInternal(r, reservationId);
    }

    /// @notice Gas-sponsored acceptance via provider's EIP-712 signature.
    function acceptReservationWithSig(
        bytes32 reservationId,
        uint256 deadline,
        bytes calldata signature
    ) external {
        if (block.timestamp > deadline) revert SignatureExpired();
        Reservation storage res = _get(reservationId);

        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                DOMAIN_SEPARATOR(),
                keccak256(
                    abi.encode(
                        ACCEPT_REQUEST_TYPEHASH,
                        reservationId,
                        res.provider,
                        nonces[res.provider]++,
                        deadline
                    )
                )
            )
        );

        address recovered = _recoverSigner(digest, signature);
        if (recovered == address(0) || recovered != res.provider) {
            revert InvalidSignature();
        }

        _acceptReservationInternal(res, reservationId);
    }

    function _acceptReservationInternal(Reservation storage r, bytes32 reservationId) private {
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

    function _recoverSigner(
        bytes32 digest,
        bytes calldata sig
    ) private pure returns (address) {
        if (sig.length != 65) return address(0);
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(sig.offset)
            s := calldataload(add(sig.offset, 32))
            v := byte(0, calldataload(add(sig.offset, 64)))
        }
        if (v < 27) v += 27;
        if (v != 27 && v != 28) return address(0);
        return ecrecover(digest, v, r, s);
    }

    function _get(
        bytes32 reservationId
    ) private view returns (Reservation storage r) {
        if (!exists[reservationId]) revert ReservationNotFound();
        return reservations[reservationId];
    }
}
