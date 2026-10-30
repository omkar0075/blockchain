// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/// @title MEDwayReservation
/// @notice On-chain commitments and institutional identity registry for MEDway healthcare resource coordination with EIP-712 meta-transaction support.
/// @dev Patient data, clinical records, and private negotiations stay strictly off-chain.
contract MEDwayReservation {
    enum Status {
        Proposed,
        Committed,
        Fulfilled,
        Cancelled,
        Expired,
        Disputed
    }

    enum InstitutionRole {
        None,
        Requester,
        Provider,
        Both
    }

    struct Institution {
        bytes32 id;
        string name;
        InstitutionRole role;
        bool isActive;
        uint64 registeredAt;
    }

    struct WalletInfo {
        bytes32 institutionId;
        bool isActive;
        uint64 authorizedAt;
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

    // Institutional Identity Registry
    mapping(bytes32 => Institution) public institutions;
    mapping(bytes32 => bool) public institutionExists;
    mapping(address => WalletInfo) public wallets;

    // Events - Reservations
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

    // Events - Institutional Registry
    event InstitutionRegistered(
        bytes32 indexed institutionId,
        string name,
        InstitutionRole role,
        uint64 registeredAt
    );
    event InstitutionStatusChanged(
        bytes32 indexed institutionId,
        bool isActive
    );
    event WalletAuthorized(
        bytes32 indexed institutionId,
        address indexed wallet,
        uint64 authorizedAt
    );
    event WalletRevoked(
        bytes32 indexed institutionId,
        address indexed wallet
    );

    // Errors
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

    // Registry Errors
    error InstitutionAlreadyExists();
    error InstitutionNotFound();
    error InstitutionInactive();
    error WalletAlreadyAuthorized();
    error WalletNotAuthorized();
    error WalletAssignedToDifferentInstitution();
    error UnauthorizedRequester();
    error UnauthorizedProvider();

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

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

    // -------------------------------------------------------------------
    // Institutional Identity Registry (Governance & Administration)
    // -------------------------------------------------------------------

    /// @notice Register a new accredited institution (Hospital or Diagnostic Provider).
    function registerInstitution(
        bytes32 institutionId,
        string calldata name,
        InstitutionRole role,
        address primaryWallet
    ) external onlyOwner {
        if (institutionId == bytes32(0)) revert InvalidEvidence();
        if (bytes(name).length == 0) revert InvalidEvidence();
        if (role == InstitutionRole.None) revert InvalidStatus();
        if (institutionExists[institutionId]) revert InstitutionAlreadyExists();

        institutionExists[institutionId] = true;
        institutions[institutionId] = Institution({
            id: institutionId,
            name: name,
            role: role,
            isActive: true,
            registeredAt: uint64(block.timestamp)
        });

        emit InstitutionRegistered(institutionId, name, role, uint64(block.timestamp));

        if (primaryWallet != address(0)) {
            _authorizeWalletInternal(institutionId, primaryWallet);
        }
    }

    /// @notice Authorize an additional or operational wallet for a registered institution.
    function authorizeWallet(
        bytes32 institutionId,
        address wallet
    ) external onlyOwner {
        if (!institutionExists[institutionId]) revert InstitutionNotFound();
        if (!institutions[institutionId].isActive) revert InstitutionInactive();
        _authorizeWalletInternal(institutionId, wallet);
    }

    function _authorizeWalletInternal(
        bytes32 institutionId,
        address wallet
    ) private {
        if (wallet == address(0)) revert InvalidAddress();
        WalletInfo storage w = wallets[wallet];

        // 1. Reject authorization if the wallet is already active (regardless of institution)
        if (w.isActive) revert WalletAlreadyAuthorized();

        // 2. Reject reassignment if the wallet historically belonged to a different institution
        // Preserves historical audit records, signature provenance, and attribution integrity
        if (w.institutionId != bytes32(0) && w.institutionId != institutionId) {
            revert WalletAssignedToDifferentInstitution();
        }

        wallets[wallet] = WalletInfo({
            institutionId: institutionId,
            isActive: true,
            authorizedAt: uint64(block.timestamp)
        });

        emit WalletAuthorized(institutionId, wallet, uint64(block.timestamp));
    }

    /// @notice Revoke a specific wallet address from an institution.
    function revokeWallet(address wallet) external onlyOwner {
        WalletInfo storage w = wallets[wallet];
        if (!w.isActive) revert WalletNotAuthorized();
        bytes32 instId = w.institutionId;
        w.isActive = false;

        emit WalletRevoked(instId, wallet);
    }

    /// @notice Suspend or re-activate an institution.
    function setInstitutionActive(bytes32 institutionId, bool isActive) external onlyOwner {
        if (!institutionExists[institutionId]) revert InstitutionNotFound();
        institutions[institutionId].isActive = isActive;
        emit InstitutionStatusChanged(institutionId, isActive);
    }

    // -------------------------------------------------------------------
    // Public Verification & Membership View Functions
    // -------------------------------------------------------------------

    /// @notice Verify whether a wallet is an active authorized requester.
    function isRequesterWallet(address wallet) public view returns (bool) {
        WalletInfo storage w = wallets[wallet];
        if (!w.isActive) return false;
        Institution storage inst = institutions[w.institutionId];
        if (!inst.isActive) return false;
        return (inst.role == InstitutionRole.Requester || inst.role == InstitutionRole.Both);
    }

    /// @notice Verify whether a wallet is an active authorized provider.
    function isProviderWallet(address wallet) public view returns (bool) {
        WalletInfo storage w = wallets[wallet];
        if (!w.isActive) return false;
        Institution storage inst = institutions[w.institutionId];
        if (!inst.isActive) return false;
        return (inst.role == InstitutionRole.Provider || inst.role == InstitutionRole.Both);
    }

    /// @notice Retrieve institution membership and active state for any wallet.
    function getInstitutionByWallet(address wallet) external view returns (
        bytes32 institutionId,
        string memory name,
        InstitutionRole role,
        bool isInstitutionActive,
        bool isWalletActive
    ) {
        WalletInfo storage w = wallets[wallet];
        if (w.institutionId == bytes32(0)) {
            return (bytes32(0), "", InstitutionRole.None, false, false);
        }
        Institution storage inst = institutions[w.institutionId];
        return (
            inst.id,
            inst.name,
            inst.role,
            inst.isActive,
            w.isActive
        );
    }

    /// @notice Retrieve institution metadata by institution ID.
    function getInstitution(bytes32 institutionId) external view returns (
        bytes32 id,
        string memory name,
        InstitutionRole role,
        bool isActive,
        uint64 registeredAt
    ) {
        if (!institutionExists[institutionId]) revert InstitutionNotFound();
        Institution storage inst = institutions[institutionId];
        return (
            inst.id,
            inst.name,
            inst.role,
            inst.isActive,
            inst.registeredAt
        );
    }

    // -------------------------------------------------------------------
    // Reservation Lifecycle (Enforced by Identity Registry)
    // -------------------------------------------------------------------

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

        // Enforce Institutional Identity Registry checks
        if (!isRequesterWallet(requester)) revert UnauthorizedRequester();
        if (!isProviderWallet(provider)) revert UnauthorizedProvider();

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
        // Enforce Provider Identity Registry check upon commitment
        if (!isProviderWallet(r.provider)) revert UnauthorizedProvider();

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
        if (!isProviderWallet(r.provider)) revert UnauthorizedProvider();
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
        // EIP-2 / OpenZeppelin ECDSA signature malleability guard (low-s check)
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E735E5A8790BA3A3984551FCD5F) {
            return address(0);
        }
        return ecrecover(digest, v, r, s);
    }

    function _get(
        bytes32 reservationId
    ) private view returns (Reservation storage r) {
        if (!exists[reservationId]) revert ReservationNotFound();
        return reservations[reservationId];
    }
}
