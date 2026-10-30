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

    enum LockStatus {
        None,
        Active,
        Converted,
        Released
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

    struct ResourceInventory {
        bytes32 resourceType;
        uint32 totalCapacity;
        uint32 lockedCapacity;
        uint32 committedCapacity;
        bool isActive;
    }

    struct CapacityLock {
        bytes32 lockId;
        bytes32 resourceType;
        address provider;
        address requester;
        uint32 quantity;
        uint64 createdAt;
        uint64 expiresAt;
        LockStatus status;
        bytes32 reservationId;
    }

    struct ReservationResourceBinding {
        bytes32 resourceType;
        address provider;
        uint32 quantity;
        bytes32 lockId;
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

    bytes32 public constant CAPACITY_LOCK_TYPEHASH = keccak256(
        "CapacityLockRequest(address requester,address provider,bytes32 resourceType,uint32 quantity,uint64 duration,uint256 nonce,uint256 deadline)"
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

    // Consortium Arbitration Board
    mapping(address => bool) public isArbitrator;
    uint256 public arbitratorCount;
    uint256 public requiredArbitrationQuorum;

    struct DisputeRuling {
        uint32 fulfilledVotes;
        uint32 cancelledVotes;
        bool isResolved;
        mapping(address => bool) hasVoted;
    }
    mapping(bytes32 => DisputeRuling) private disputeRulings;

    // Resource & Capacity Inventory (Feature 3)
    // providerAddress => resourceType => ResourceInventory
    mapping(address => mapping(bytes32 => ResourceInventory)) public providerInventories;
    mapping(bytes32 => CapacityLock) public capacityLocks;
    mapping(bytes32 => bool) public lockExists;
    mapping(bytes32 => ReservationResourceBinding) public reservationBindings;

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
    event InstitutionRoleUpdated(
        bytes32 indexed institutionId,
        InstitutionRole previousRole,
        InstitutionRole newRole
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
    event ReservationWalletRotated(
        bytes32 indexed reservationId,
        address indexed oldWallet,
        address indexed newWallet,
        bool isProvider
    );

    // Events - Consortium Arbitration
    event ArbitratorAdded(address indexed arbitrator);
    event ArbitratorRemoved(address indexed arbitrator);
    event ArbitrationQuorumUpdated(uint256 newQuorum);
    event DisputeVoteCast(
        bytes32 indexed reservationId,
        address indexed arbitrator,
        Status ruling,
        bytes32 rulingEvidence
    );

    // Events - Capacity Management & Locking (Feature 3)
    event ResourceInventorySet(
        address indexed provider,
        bytes32 indexed resourceType,
        uint32 totalCapacity,
        bool isActive
    );

    event CapacityLocked(
        bytes32 indexed lockId,
        address indexed provider,
        address indexed requester,
        bytes32 resourceType,
        uint32 quantity,
        uint64 expiresAt
    );

    event CapacityLockConverted(
        bytes32 indexed lockId,
        bytes32 indexed reservationId,
        uint32 quantity
    );

    event CapacityLockReleased(
        bytes32 indexed lockId,
        address indexed provider,
        uint32 quantity,
        string reason
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
    error InvalidRole();
    error WalletAlreadyAuthorized();
    error WalletNotAuthorized();
    error WalletAssignedToDifferentInstitution();
    error UnauthorizedRequester();
    error UnauthorizedProvider();

    // Arbitration Errors
    error ArbitratorAlreadyAdded();
    error ArbitratorNotFound();
    error InvalidQuorum();
    error AlreadyVoted();

    // Capacity & Locking Errors (Feature 3)
    error InsufficientCapacity();
    error ResourceNotActive();
    error LockNotFound();
    error LockNotActive();
    error LockExpired();
    error InvalidLockQuantity();

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    modifier onlyArbitrator() {
        if (!isArbitrator[msg.sender] && msg.sender != owner) revert Unauthorized();
        _;
    }

    constructor() {
        owner = msg.sender;
        isArbitrator[msg.sender] = true;
        arbitratorCount = 1;
        requiredArbitrationQuorum = 1;
        emit ArbitratorAdded(msg.sender);

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

    /// @notice Update the accredited operational role of a registered institution.
    /// @dev Allows owner to expand or alter an institution's capabilities (e.g. Provider -> Both).
    function setInstitutionRole(bytes32 institutionId, InstitutionRole newRole) external onlyOwner {
        if (!institutionExists[institutionId]) revert InstitutionNotFound();
        if (newRole == InstitutionRole.None) revert InvalidRole();

        Institution storage inst = institutions[institutionId];
        InstitutionRole prevRole = inst.role;
        if (prevRole == newRole) revert InvalidRole();

        inst.role = newRole;
        emit InstitutionRoleUpdated(institutionId, prevRole, newRole);
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
    // Consortium Arbitration Governance
    // -------------------------------------------------------------------

    /// @notice Add a new arbitrator to the consortium board.
    function addArbitrator(address arbitrator) external onlyOwner {
        if (arbitrator == address(0)) revert InvalidAddress();
        if (isArbitrator[arbitrator]) revert ArbitratorAlreadyAdded();

        isArbitrator[arbitrator] = true;
        arbitratorCount++;
        emit ArbitratorAdded(arbitrator);
    }

    /// @notice Remove an arbitrator from the consortium board.
    function removeArbitrator(address arbitrator) external onlyOwner {
        if (!isArbitrator[arbitrator]) revert ArbitratorNotFound();
        if (arbitratorCount - 1 < requiredArbitrationQuorum) revert InvalidQuorum();

        isArbitrator[arbitrator] = false;
        arbitratorCount--;
        emit ArbitratorRemoved(arbitrator);
    }

    /// @notice Set the required voting quorum for dispute resolution.
    function setArbitrationQuorum(uint256 newQuorum) external onlyOwner {
        if (newQuorum == 0 || newQuorum > arbitratorCount) revert InvalidQuorum();
        requiredArbitrationQuorum = newQuorum;
        emit ArbitrationQuorumUpdated(newQuorum);
    }

    // -------------------------------------------------------------------
    // Resource Inventory & Capacity Locking (Feature 3)
    // -------------------------------------------------------------------

    /// @notice Provider registers or updates capacity quota for a resource type.
    /// @dev Only active provider institution wallets can configure their own capacity.
    function setResourceInventory(
        bytes32 resourceType,
        uint32 totalCapacity,
        bool isActive
    ) external {
        if (!isProviderWallet(msg.sender)) revert UnauthorizedProvider();
        if (resourceType == bytes32(0)) revert InvalidEvidence();

        ResourceInventory storage inv = providerInventories[msg.sender][resourceType];
        // Cannot reduce total capacity below currently locked + committed capacity
        if (totalCapacity < inv.lockedCapacity + inv.committedCapacity) {
            revert InsufficientCapacity();
        }

        inv.resourceType = resourceType;
        inv.totalCapacity = totalCapacity;
        inv.isActive = isActive;

        emit ResourceInventorySet(msg.sender, resourceType, totalCapacity, isActive);
    }

    /// @notice Deterministic query calculating real-time unallocated capacity on-chain.
    function getAvailableCapacity(address provider, bytes32 resourceType) public view returns (uint32) {
        ResourceInventory storage inv = providerInventories[provider][resourceType];
        if (!inv.isActive) return 0;
        uint32 inUse = inv.lockedCapacity + inv.committedCapacity;
        if (inUse >= inv.totalCapacity) return 0;
        return inv.totalCapacity - inUse;
    }

    /// @notice Retrieve full inventory status for a provider's resource.
    function getResourceInventory(address provider, bytes32 resourceType) external view returns (
        uint32 totalCapacity,
        uint32 lockedCapacity,
        uint32 committedCapacity,
        uint32 availableCapacity,
        bool isActive
    ) {
        ResourceInventory storage inv = providerInventories[provider][resourceType];
        uint32 available = getAvailableCapacity(provider, resourceType);
        return (inv.totalCapacity, inv.lockedCapacity, inv.committedCapacity, available, inv.isActive);
    }

    /// @notice Direct capacity locking by an accredited requester hospital.
    function lockCapacity(
        address provider,
        bytes32 resourceType,
        uint32 quantity,
        uint64 duration
    ) external returns (bytes32 lockId) {
        return _lockCapacityInternal(msg.sender, provider, resourceType, quantity, duration);
    }

    /// @notice Gas-sponsored capacity locking via EIP-712 typed signature.
    function lockCapacityWithSig(
        address requester,
        address provider,
        bytes32 resourceType,
        uint32 quantity,
        uint64 duration,
        uint256 deadline,
        bytes calldata signature
    ) external returns (bytes32 lockId) {
        if (block.timestamp > deadline) revert SignatureExpired();
        if (requester == address(0)) revert InvalidAddress();

        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                DOMAIN_SEPARATOR(),
                keccak256(
                    abi.encode(
                        CAPACITY_LOCK_TYPEHASH,
                        requester,
                        provider,
                        resourceType,
                        quantity,
                        duration,
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

        return _lockCapacityInternal(requester, provider, resourceType, quantity, duration);
    }

    function _lockCapacityInternal(
        address requester,
        address provider,
        bytes32 resourceType,
        uint32 quantity,
        uint64 duration
    ) private returns (bytes32 lockId) {
        if (!isRequesterWallet(requester)) revert UnauthorizedRequester();
        if (!isProviderWallet(provider)) revert UnauthorizedProvider();
        if (quantity == 0) revert InvalidLockQuantity();
        if (duration == 0 || duration > 7 days) revert InvalidExpiry();

        ResourceInventory storage inv = providerInventories[provider][resourceType];
        if (!inv.isActive) revert ResourceNotActive();

        uint32 available = getAvailableCapacity(provider, resourceType);
        if (quantity > available) revert InsufficientCapacity();

        // Increment locked capacity
        inv.lockedCapacity += quantity;

        uint64 expiresAt = uint64(block.timestamp) + duration;
        lockId = keccak256(
            abi.encodePacked(
                requester,
                provider,
                resourceType,
                quantity,
                block.timestamp,
                nextNonce++
            )
        );

        lockExists[lockId] = true;
        capacityLocks[lockId] = CapacityLock({
            lockId: lockId,
            resourceType: resourceType,
            provider: provider,
            requester: requester,
            quantity: quantity,
            createdAt: uint64(block.timestamp),
            expiresAt: expiresAt,
            status: LockStatus.Active,
            reservationId: bytes32(0)
        });

        emit CapacityLocked(
            lockId,
            provider,
            requester,
            resourceType,
            quantity,
            expiresAt
        );
    }

    /// @notice Atomically converts an active capacity lock into an on-chain reservation commitment.
    /// @dev Transfers quantity from lockedCapacity to committedCapacity, preventing any capacity gap or double-allocation.
    function createReservationWithLock(
        bytes32 lockId,
        bytes32 resourceRef,
        uint64 expiresAt
    ) external returns (bytes32 reservationId) {
        if (!lockExists[lockId]) revert LockNotFound();
        CapacityLock storage lock = capacityLocks[lockId];

        if (lock.status != LockStatus.Active) revert LockNotActive();
        if (block.timestamp > lock.expiresAt) revert LockExpired();

        // Caller must be requester, authorized institution wallet, or relayer/owner
        if (msg.sender != lock.requester && msg.sender != owner) {
            if (!_isRequesterInstitutionWalletInternal(lock.requester, msg.sender)) {
                revert Unauthorized();
            }
        }

        // Shift capacity: locked -> committed
        ResourceInventory storage inv = providerInventories[lock.provider][lock.resourceType];
        inv.lockedCapacity -= lock.quantity;
        inv.committedCapacity += lock.quantity;

        // Create the underlying reservation
        reservationId = _createReservationInternal(lock.requester, lock.provider, resourceRef, expiresAt);

        // Update lock state
        lock.status = LockStatus.Converted;
        lock.reservationId = reservationId;

        // Bind reservation to resource inventory so resolution/expiry releases committed capacity
        reservationBindings[reservationId] = ReservationResourceBinding({
            resourceType: lock.resourceType,
            provider: lock.provider,
            quantity: lock.quantity,
            lockId: lockId
        });

        emit CapacityLockConverted(lockId, reservationId, lock.quantity);
    }

    /// @notice Release an uncommitted capacity lock back to available inventory.
    /// @dev Can be called by requester (e.g. negotiation declined) or anyone if expired.
    function releaseLock(bytes32 lockId, string calldata reason) external {
        if (!lockExists[lockId]) revert LockNotFound();
        CapacityLock storage lock = capacityLocks[lockId];

        if (lock.status != LockStatus.Active) revert LockNotActive();

        bool isExpired = block.timestamp > lock.expiresAt;
        bool isRequester = msg.sender == lock.requester || _isRequesterInstitutionWalletInternal(lock.requester, msg.sender);
        bool isProvider = msg.sender == lock.provider || _isProviderInstitutionWalletInternal(lock.provider, msg.sender);
        bool isOwner = msg.sender == owner;

        if (!isExpired && !isRequester && !isProvider && !isOwner) {
            revert Unauthorized();
        }

        // Release locked capacity
        ResourceInventory storage inv = providerInventories[lock.provider][lock.resourceType];
        if (inv.lockedCapacity >= lock.quantity) {
            inv.lockedCapacity -= lock.quantity;
        } else {
            inv.lockedCapacity = 0;
        }

        lock.status = LockStatus.Released;
        emit CapacityLockReleased(lockId, lock.provider, lock.quantity, reason);
    }

    /// @notice Helper to release committed capacity when a reservation completes, cancels, or expires.
    function _releaseCommittedCapacity(bytes32 reservationId) private {
        ReservationResourceBinding storage binding = reservationBindings[reservationId];
        if (binding.quantity > 0) {
            ResourceInventory storage inv = providerInventories[binding.provider][binding.resourceType];
            if (inv.committedCapacity >= binding.quantity) {
                inv.committedCapacity -= binding.quantity;
            } else {
                inv.committedCapacity = 0;
            }
            binding.quantity = 0;
        }
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
    /// @dev Can be called by the designated provider wallet or any active authorized wallet of the provider institution.
    ///      Ensures that wallet revocation/rotation does not prevent fulfillment of existing commitments.
    function fulfillReservation(bytes32 reservationId) external {
        Reservation storage r = _get(reservationId);
        if (!_isProviderInstitutionWallet(r, msg.sender)) revert UnauthorizedProvider();
        if (r.status != Status.Committed) revert InvalidStatus();
        if (block.timestamp > r.expiresAt) revert InvalidExpiry();

        r.status = Status.Fulfilled;
        r.fulfilledAt = uint64(block.timestamp);
        _releaseCommittedCapacity(reservationId);
        emit ReservationFulfilled(reservationId, r.fulfilledAt);
    }

    /// @notice Requester or provider can cancel a proposal or commitment before expiry.
    /// @dev Supports active replacement wallets of either institution if an initial wallet was revoked.
    function cancelReservation(bytes32 reservationId) external {
        Reservation storage r = _get(reservationId);
        bool isReq = _isRequesterInstitutionWallet(r, msg.sender);
        bool isProv = _isProviderInstitutionWallet(r, msg.sender);
        if (!isReq && !isProv) {
            revert Unauthorized();
        }
        if (
            r.status != Status.Proposed &&
            r.status != Status.Committed
        ) revert InvalidStatus();
        if (block.timestamp >= r.expiresAt) revert InvalidExpiry();

        r.status = Status.Cancelled;
        _releaseCommittedCapacity(reservationId);
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
        _releaseCommittedCapacity(reservationId);
        emit ReservationExpired(reservationId);
    }

    /// @notice Either party can raise a dispute using a non-sensitive evidence hash.
    /// @dev If already fulfilled, dispute must be initiated within DISPUTE_WINDOW.
    function disputeReservation(
        bytes32 reservationId,
        bytes32 evidenceHash
    ) external {
        Reservation storage r = _get(reservationId);
        bool isReq = _isRequesterInstitutionWallet(r, msg.sender);
        bool isProv = _isProviderInstitutionWallet(r, msg.sender);
        if (!isReq && !isProv) {
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

    /// @notice Reassign an outstanding reservation's designated wallet to an active authorized wallet of the same institution.
    /// @dev Allows institutions or contract owner to rotate keys for outstanding commitments without disrupting commitments.
    function rotateReservationWallet(
        bytes32 reservationId,
        address newWallet
    ) external {
        Reservation storage r = _get(reservationId);
        if (r.status != Status.Proposed && r.status != Status.Committed) {
            revert InvalidStatus();
        }
        if (newWallet == address(0)) revert InvalidAddress();

        WalletInfo storage newW = wallets[newWallet];
        if (!newW.isActive) revert WalletNotAuthorized();

        bytes32 reqInstId = wallets[r.requester].institutionId;
        bytes32 provInstId = wallets[r.provider].institutionId;

        if (newW.institutionId == provInstId) {
            if (!_isProviderInstitutionWallet(r, msg.sender) && msg.sender != owner) {
                revert Unauthorized();
            }
            if (!isProviderWallet(newWallet)) revert UnauthorizedProvider();
            address oldWallet = r.provider;
            r.provider = newWallet;
            emit ReservationWalletRotated(reservationId, oldWallet, newWallet, true);
        } else if (newW.institutionId == reqInstId) {
            if (!_isRequesterInstitutionWallet(r, msg.sender) && msg.sender != owner) {
                revert Unauthorized();
            }
            if (!isRequesterWallet(newWallet)) revert UnauthorizedRequester();
            address oldWallet = r.requester;
            r.requester = newWallet;
            emit ReservationWalletRotated(reservationId, oldWallet, newWallet, false);
        } else {
            revert WalletAssignedToDifferentInstitution();
        }
    }

    /// @notice Check if caller is an active authorized wallet representing the requester institution of a reservation.
    function _isRequesterInstitutionWallet(Reservation storage r, address caller) private view returns (bool) {
        return _isRequesterInstitutionWalletInternal(r.requester, caller);
    }

    function _isRequesterInstitutionWalletInternal(address targetRequester, address caller) private view returns (bool) {
        if (caller == targetRequester && isRequesterWallet(caller)) return true;
        bytes32 reqInstId = wallets[targetRequester].institutionId;
        if (reqInstId == bytes32(0)) return false;
        WalletInfo storage w = wallets[caller];
        return (w.isActive && w.institutionId == reqInstId && institutions[reqInstId].isActive);
    }

    /// @notice Check if caller is an active authorized wallet representing the provider institution of a reservation.
    function _isProviderInstitutionWallet(Reservation storage r, address caller) private view returns (bool) {
        return _isProviderInstitutionWalletInternal(r.provider, caller);
    }

    function _isProviderInstitutionWalletInternal(address targetProvider, address caller) private view returns (bool) {
        if (caller == targetProvider && isProviderWallet(caller)) return true;
        bytes32 provInstId = wallets[targetProvider].institutionId;
        if (provInstId == bytes32(0)) return false;
        WalletInfo storage w = wallets[caller];
        return (w.isActive && w.institutionId == provInstId && institutions[provInstId].isActive);
    }

    /// @notice Cast a vote on a disputed reservation. If votes reach quorum, dispute is automatically finalized.
    function castArbitrationVote(
        bytes32 reservationId,
        Status ruling,
        bytes32 rulingEvidence
    ) external onlyArbitrator {
        Reservation storage r = _get(reservationId);
        if (r.status != Status.Disputed) revert InvalidStatus();
        if (ruling != Status.Fulfilled && ruling != Status.Cancelled) revert InvalidStatus();
        if (rulingEvidence == bytes32(0)) revert InvalidEvidence();

        DisputeRuling storage dr = disputeRulings[reservationId];
        if (dr.isResolved) revert InvalidStatus();
        if (dr.hasVoted[msg.sender]) revert AlreadyVoted();

        dr.hasVoted[msg.sender] = true;
        emit DisputeVoteCast(reservationId, msg.sender, ruling, rulingEvidence);

        if (ruling == Status.Fulfilled) {
            dr.fulfilledVotes++;
            if (dr.fulfilledVotes >= requiredArbitrationQuorum) {
                dr.isResolved = true;
                r.status = Status.Fulfilled;
                _releaseCommittedCapacity(reservationId);
                emit ReservationDisputeResolved(
                    reservationId,
                    msg.sender,
                    Status.Fulfilled,
                    rulingEvidence
                );
            }
        } else if (ruling == Status.Cancelled) {
            dr.cancelledVotes++;
            if (dr.cancelledVotes >= requiredArbitrationQuorum) {
                dr.isResolved = true;
                r.status = Status.Cancelled;
                _releaseCommittedCapacity(reservationId);
                emit ReservationDisputeResolved(
                    reservationId,
                    msg.sender,
                    Status.Cancelled,
                    rulingEvidence
                );
            }
        }
    }

    /// @notice Authorized consortium owner/arbitrator resolves an active dispute directly when quorum is 1.
    function resolveDispute(
        bytes32 reservationId,
        Status resolution,
        bytes32 resolutionEvidence
    ) external onlyArbitrator {
        Reservation storage r = _get(reservationId);
        if (r.status != Status.Disputed) revert InvalidStatus();
        if (
            resolution != Status.Fulfilled &&
            resolution != Status.Cancelled
        ) revert InvalidStatus();
        if (resolutionEvidence == bytes32(0)) revert InvalidEvidence();
        if (requiredArbitrationQuorum > 1) {
            revert InvalidQuorum();
        }

        DisputeRuling storage dr = disputeRulings[reservationId];
        dr.isResolved = true;
        if (resolution == Status.Fulfilled) dr.fulfilledVotes++;
        else dr.cancelledVotes++;
        dr.hasVoted[msg.sender] = true;

        r.status = resolution;
        _releaseCommittedCapacity(reservationId);
        emit DisputeVoteCast(reservationId, msg.sender, resolution, resolutionEvidence);
        emit ReservationDisputeResolved(
            reservationId,
            msg.sender,
            resolution,
            resolutionEvidence
        );
    }

    /// @notice Retrieve arbitration voting state for a reservation.
    function getDisputeRuling(bytes32 reservationId) external view returns (
        uint32 fulfilledVotes,
        uint32 cancelledVotes,
        bool isResolved
    ) {
        DisputeRuling storage dr = disputeRulings[reservationId];
        return (dr.fulfilledVotes, dr.cancelledVotes, dr.isResolved);
    }

    /// @notice Check if a specific arbitrator has voted on a reservation dispute.
    function hasArbitratorVoted(bytes32 reservationId, address arbitrator) external view returns (bool) {
        return disputeRulings[reservationId].hasVoted[arbitrator];
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
