const fs = require('fs');
const path = require('path');
const {
  createPublicClient,
  createWalletClient,
  http,
  getContract,
  parseEventLogs,
  pad,
  stringToHex,
  isHex,
  parseEther,
} = require('viem');
const { privateKeyToAccount } = require('viem/accounts');

// 1. Load configuration from .env
function loadEnv() {
  const envCandidates = [
    path.join(__dirname, '.env'),
    path.join(__dirname, '../contracts/.env'),
  ];

  for (const envPath of envCandidates) {
    if (fs.existsSync(envPath)) {
      const content = fs.readFileSync(envPath, 'utf8');
      for (const line of content.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (trimmed && !trimmed.startsWith('#')) {
          const eqIdx = trimmed.indexOf('=');
          if (eqIdx !== -1) {
            const key = trimmed.slice(0, eqIdx).trim();
            const val = trimmed.slice(eqIdx + 1).trim();
            if (!process.env[key]) {
              process.env[key] = val;
            }
          }
        }
      }
    }
  }
}

loadEnv();

const RPC_URL = process.env.CORAL_RPC_URL || 'https://coralrpc.mstblockchain.com';
const CHAIN_ID = parseInt(process.env.CHAIN_ID || '54635', 10);
const CONTRACT_ADDRESS =
  process.env.CONTRACT_ADDRESS ||
  process.env.MEDWAY_CONTRACT_ADDRESS ||
  '0xcd630b8e555e0751afbb9ae511869110d361bb83';

// Relayer private key (optional: if omitted, write calls report relayer unconfigured)
const RELAYER_KEY =
  process.env.CORAL_PRIVATE_KEY ||
  process.env.RELAYER_PRIVATE_KEY ||
  process.env.SPONSOR_PRIVATE_KEY;

// Known persistent institution keys / addresses
const INSTITUTIONS = {
  'hospital-a': {
    name: 'City Hospital A',
    key: process.env.HOSPITAL_A_PRIVATE_KEY,
    address: process.env.HOSPITAL_A_ADDRESS || '0x4dB8336edB0af7FeD1311312081689993F80a18B',
  },
  'lab-c': {
    name: 'Central Diagnostic Lab C',
    key: process.env.LAB_C_PRIVATE_KEY,
    address: process.env.LAB_C_ADDRESS || '0xb05049cc2A5a1c28979DEafc82B82749681DA92C',
  },
};

const STATUS_NAMES = [
  'Proposed',
  'Committed',
  'Fulfilled',
  'Cancelled',
  'Expired',
  'Disputed',
];

const coralChain = {
  id: CHAIN_ID,
  name: 'Coral Testnet',
  nativeCurrency: { name: 'MST', symbol: 'MST', decimals: 18 },
  rpcUrls: {
    default: { http: [RPC_URL] },
  },
};

function getPublicClient() {
  return createPublicClient({
    chain: coralChain,
    transport: http(RPC_URL),
  });
}

function getRelayerClient() {
  if (!RELAYER_KEY || !isHex(RELAYER_KEY) || RELAYER_KEY.length !== 66) {
    throw new Error(
      'Relayer private key is not configured or invalid. Set CORAL_PRIVATE_KEY or RELAYER_PRIVATE_KEY in backend/.env.'
    );
  }
  const relayerAccount = privateKeyToAccount(RELAYER_KEY);
  return {
    account: relayerAccount,
    client: createWalletClient({
      account: relayerAccount,
      chain: coralChain,
      transport: http(RPC_URL),
    }),
  };
}

function loadArtifact() {
  const artifactPath = path.join(
    __dirname,
    '../contracts/artifacts/contracts/MEDwayReservation.sol/MEDwayReservation.json'
  );
  if (!fs.existsSync(artifactPath)) {
    throw new Error(`Artifact not found at ${artifactPath}. Run 'npx hardhat build' in contracts.`);
  }
  return JSON.parse(fs.readFileSync(artifactPath, 'utf8'));
}

function resolveAccount(identifier) {
  if (INSTITUTIONS[identifier]) {
    const inst = INSTITUTIONS[identifier];
    if (inst.key && isHex(inst.key)) {
      return privateKeyToAccount(inst.key);
    }
    throw new Error(`Institution '${identifier}' key not found in environment.`);
  }
  if (isHex(identifier) && identifier.length === 66) {
    return privateKeyToAccount(identifier);
  }
  throw new Error(`Cannot resolve private key for identifier: '${identifier}'.`);
}

function resolveAddress(identifier) {
  if (INSTITUTIONS[identifier]) {
    return INSTITUTIONS[identifier].address;
  }
  if (isHex(identifier) && identifier.length === 42) {
    return identifier;
  }
  throw new Error(`Cannot resolve address for identifier: '${identifier}'.`);
}

function jsonOut(data) {
  const serialized = JSON.stringify(
    data,
    (k, v) => (typeof v === 'bigint' ? v.toString() : v),
    2
  );
  console.log(serialized);
}

// -------------------------------------------------------------------
// Commands
// -------------------------------------------------------------------

async function cmdStatus() {
  const client = getPublicClient();
  const artifact = loadArtifact();
  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client,
  });

  const [blockNumber, owner, disputeWindow, domainSeparator] = await Promise.all([
    client.getBlockNumber(),
    contract.read.owner(),
    contract.read.DISPUTE_WINDOW(),
    contract.read.DOMAIN_SEPARATOR(),
  ]);

  let relayerInfo = { configured: false };
  if (RELAYER_KEY && isHex(RELAYER_KEY) && RELAYER_KEY.length === 66) {
    try {
      const relayerAccount = privateKeyToAccount(RELAYER_KEY);
      const balance = await client.getBalance({ address: relayerAccount.address });
      relayerInfo = {
        configured: true,
        address: relayerAccount.address,
        balanceWei: balance.toString(),
      };
    } catch {
      relayerInfo = { configured: false, error: 'Failed to derive relayer address' };
    }
  }

  jsonOut({
    success: true,
    connected: true,
    network: 'Coral Testnet',
    chainId: CHAIN_ID,
    rpcUrl: RPC_URL,
    contractAddress: CONTRACT_ADDRESS,
    latestBlock: Number(blockNumber),
    contractOwner: owner,
    disputeWindowSeconds: Number(disputeWindow),
    domainSeparator,
    relayer: relayerInfo,
    registeredInstitutions: {
      'hospital-a': {
        name: INSTITUTIONS['hospital-a'].name,
        address: INSTITUTIONS['hospital-a'].address,
        hasKey: Boolean(INSTITUTIONS['hospital-a'].key),
      },
      'lab-c': {
        name: INSTITUTIONS['lab-c'].name,
        address: INSTITUTIONS['lab-c'].address,
        hasKey: Boolean(INSTITUTIONS['lab-c'].key),
      },
    },
  });
}

async function cmdCreateReservation(args) {
  const requesterId = args.requester || 'hospital-a';
  const providerId = args.provider || 'lab-c';
  const resourceRefRaw = args.resourceRef || `slot-${Date.now()}`;
  const expiresIn = BigInt(args.expiresIn || 7200);

  const requesterAccount = resolveAccount(requesterId);
  const providerAddress = resolveAddress(providerId);

  const client = getPublicClient();
  const { account: relayerAccount, client: walletClient } = getRelayerClient();
  const artifact = loadArtifact();

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: walletClient },
  });

  const latestBlock = await client.getBlock();
  const expiresAt = latestBlock.timestamp + expiresIn;
  const deadline = latestBlock.timestamp + 1800n; // 30 mins
  const nonce = await contract.read.nonces([requesterAccount.address]);

  const resourceRef = resourceRefRaw.startsWith('0x') && resourceRefRaw.length === 66
    ? resourceRefRaw
    : pad(stringToHex(resourceRefRaw), { size: 32 });

  const domain = {
    name: 'MEDwayReservation',
    version: '1',
    chainId: CHAIN_ID,
    verifyingContract: CONTRACT_ADDRESS,
  };

  const types = {
    ReservationRequest: [
      { name: 'requester', type: 'address' },
      { name: 'provider', type: 'address' },
      { name: 'resourceRef', type: 'bytes32' },
      { name: 'expiresAt', type: 'uint64' },
      { name: 'nonce', type: 'uint256' },
      { name: 'deadline', type: 'uint256' },
    ],
  };

  // 1. Off-chain EIP-712 signature by requester
  const signature = await requesterAccount.signTypedData({
    domain,
    types,
    primaryType: 'ReservationRequest',
    message: {
      requester: requesterAccount.address,
      provider: providerAddress,
      resourceRef,
      expiresAt,
      nonce,
      deadline,
    },
  });

  // 2. Relayer submits transaction
  const txHash = await contract.write.createReservationWithSig(
    [
      requesterAccount.address,
      providerAddress,
      resourceRef,
      expiresAt,
      deadline,
      signature,
    ],
    { account: relayerAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  // 3. Decode event from receipt.logs
  const createdLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'ReservationCreated',
  });

  if (createdLogs.length === 0) {
    throw new Error('No ReservationCreated event found in transaction receipt logs.');
  }

  const reservationId = createdLogs[0].args.reservationId;

  jsonOut({
    success: true,
    action: 'create-reservation',
    reservationId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    requester: requesterAccount.address,
    provider: providerAddress,
    resourceRef,
    expiresAt: expiresAt.toString(),
    relayer: relayerAccount.address,
  });
}

async function cmdAcceptReservation(args) {
  const reservationId = args.reservationId;
  if (!reservationId || !isHex(reservationId) || reservationId.length !== 66) {
    throw new Error('Missing or invalid reservationId (must be 0x... 32-byte hex).');
  }

  const providerId = args.provider || 'lab-c';
  const providerAccount = resolveAccount(providerId);

  const client = getPublicClient();
  const { account: relayerAccount, client: walletClient } = getRelayerClient();
  const artifact = loadArtifact();

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: walletClient },
  });

  const latestBlock = await client.getBlock();
  const deadline = latestBlock.timestamp + 1800n;
  const nonce = await contract.read.nonces([providerAccount.address]);

  const domain = {
    name: 'MEDwayReservation',
    version: '1',
    chainId: CHAIN_ID,
    verifyingContract: CONTRACT_ADDRESS,
  };

  const types = {
    AcceptRequest: [
      { name: 'reservationId', type: 'bytes32' },
      { name: 'provider', type: 'address' },
      { name: 'nonce', type: 'uint256' },
      { name: 'deadline', type: 'uint256' },
    ],
  };

  // 1. Off-chain EIP-712 acceptance signature by provider
  const signature = await providerAccount.signTypedData({
    domain,
    types,
    primaryType: 'AcceptRequest',
    message: {
      reservationId,
      provider: providerAccount.address,
      nonce,
      deadline,
    },
  });

  // 2. Relayer submits transaction
  const txHash = await contract.write.acceptReservationWithSig(
    [reservationId, deadline, signature],
    { account: relayerAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const acceptLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'ReservationCommitted',
  });

  if (acceptLogs.length === 0) {
    throw new Error('No ReservationCommitted event found in transaction receipt logs.');
  }

  jsonOut({
    success: true,
    action: 'accept-reservation',
    reservationId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    provider: providerAccount.address,
    status: 'Committed',
    relayer: relayerAccount.address,
  });
}

async function ensureGasWithLock(targetAddress, client) {
  const minRequired = parseEther('0.005');
  const balance = await client.getBalance({ address: targetAddress });
  if (balance >= minRequired) {
    return null; // Already sufficient
  }

  // Acquire atomic filesystem lock to prevent concurrent top-up race conditions
  const locksDir = path.join(__dirname, '.locks');
  if (!fs.existsSync(locksDir)) {
    fs.mkdirSync(locksDir, { recursive: true });
  }
  const lockFile = path.join(locksDir, `topup-${targetAddress.toLowerCase()}.lock`);

  const maxWait = 25000;
  const start = Date.now();
  let fd = null;

  while (fd === null && Date.now() - start < maxWait) {
    try {
      fd = fs.openSync(lockFile, 'wx');
      fs.writeSync(fd, `pid:${process.pid}\ntime:${Date.now()}`);
    } catch (err) {
      if (err.code === 'EEXIST') {
        try {
          const stats = fs.statSync(lockFile);
          if (Date.now() - stats.mtimeMs > 35000) {
            fs.unlinkSync(lockFile);
          }
        } catch {}
        const waitTill = Date.now() + 250;
        while (Date.now() < waitTill) {}
      } else {
        throw err;
      }
    }
  }

  if (fd === null) {
    throw new Error(`Timeout acquiring gas top-up lock for ${targetAddress}`);
  }

  try {
    // Re-check balance inside critical section (double-check locking pattern)
    const freshBalance = await client.getBalance({ address: targetAddress });
    if (freshBalance >= minRequired) {
      return null; // Already funded by concurrent caller
    }

    const { account: relayerAccount, client: relayerWallet } = getRelayerClient();
    const topUpHash = await relayerWallet.sendTransaction({
      to: targetAddress,
      value: parseEther('0.01'),
    });
    await client.waitForTransactionReceipt({ hash: topUpHash });
    return topUpHash;
  } finally {
    try { fs.closeSync(fd); } catch {}
    try { fs.unlinkSync(lockFile); } catch {}
  }
}

async function cmdFulfillReservation(args) {
  const reservationId = args.reservationId;
  if (!reservationId || !isHex(reservationId) || reservationId.length !== 66) {
    throw new Error('Missing or invalid reservationId (must be 0x... 32-byte hex).');
  }

  const providerId = args.provider || 'lab-c';
  const providerAccount = resolveAccount(providerId);

  const client = getPublicClient();
  const artifact = loadArtifact();

  // 1. Concurrency-safe gas top-up check
  const topUpTxHash = await ensureGasWithLock(providerAccount.address, client);

  // 2. Provider signs & submits fulfillReservation directly as msg.sender
  const providerWallet = createWalletClient({
    account: providerAccount,
    chain: coralChain,
    transport: http(RPC_URL),
  });

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: providerWallet },
  });

  const txHash = await contract.write.fulfillReservation([reservationId], {
    account: providerAccount,
  });

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const fulfilledLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'ReservationFulfilled',
  });

  if (fulfilledLogs.length === 0) {
    throw new Error('No ReservationFulfilled event found in transaction receipt logs.');
  }

  const fulfilledAt = fulfilledLogs[0].args.fulfilledAt.toString();

  jsonOut({
    success: true,
    action: 'fulfill-reservation',
    reservationId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    provider: providerAccount.address,
    fulfilledAt,
    status: 'Fulfilled',
    relayerTopUpTx: topUpTxHash,
  });
}

async function cmdDisputeReservation(args) {
  const reservationId = args.reservationId;
  if (!reservationId || !isHex(reservationId) || reservationId.length !== 66) {
    throw new Error('Missing or invalid reservationId (must be 0x... 32-byte hex).');
  }

  const actorId = args.actor || 'hospital-a';
  const actorAccount = resolveAccount(actorId);

  let evidenceHash = args.evidenceHash;
  if (!evidenceHash) {
    const reason = args.reason || `Dispute raised by ${actorId} at ${Date.now()}`;
    evidenceHash = pad(stringToHex(reason.slice(0, 31)), { size: 32 });
  } else if (!isHex(evidenceHash) || evidenceHash.length !== 66) {
    evidenceHash = pad(stringToHex(evidenceHash.slice(0, 31)), { size: 32 });
  }

  const client = getPublicClient();
  const artifact = loadArtifact();

  // 1. Concurrency-safe gas top-up check for disputing actor
  const topUpTxHash = await ensureGasWithLock(actorAccount.address, client);

  // 2. Actor executes disputeReservation directly as msg.sender
  const actorWallet = createWalletClient({
    account: actorAccount,
    chain: coralChain,
    transport: http(RPC_URL),
  });

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: actorWallet },
  });

  const txHash = await contract.write.disputeReservation(
    [reservationId, evidenceHash],
    { account: actorAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const disputeLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'ReservationDisputed',
  });

  if (disputeLogs.length === 0) {
    throw new Error('No ReservationDisputed event found in transaction receipt logs.');
  }

  jsonOut({
    success: true,
    action: 'dispute-reservation',
    reservationId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    disputedBy: actorAccount.address,
    evidenceHash,
    status: 'Disputed',
    relayerTopUpTx: topUpTxHash,
  });
}

async function cmdResolveDispute(args) {
  const reservationId = args.reservationId;
  if (!reservationId || !isHex(reservationId) || reservationId.length !== 66) {
    throw new Error('Missing or invalid reservationId.');
  }

  const resolutionRaw = (args.resolution || 'Cancelled').toLowerCase();
  let resolutionCode;
  if (resolutionRaw === 'fulfilled' || resolutionRaw === '2') {
    resolutionCode = 2;
  } else if (resolutionRaw === 'cancelled' || resolutionRaw === 'canceled' || resolutionRaw === '3') {
    resolutionCode = 3;
  } else {
    throw new Error("Invalid resolution outcome. Must be 'Fulfilled' or 'Cancelled'.");
  }

  let resolutionEvidence = args.resolutionEvidence;
  if (!resolutionEvidence) {
    const memo = args.notes || `Arbitration resolved to ${STATUS_NAMES[resolutionCode]} at ${Date.now()}`;
    resolutionEvidence = pad(stringToHex(memo.slice(0, 31)), { size: 32 });
  } else if (!isHex(resolutionEvidence) || resolutionEvidence.length !== 66) {
    resolutionEvidence = pad(stringToHex(resolutionEvidence.slice(0, 31)), { size: 32 });
  }

  const client = getPublicClient();
  const { account: relayerAccount, client: relayerWallet } = getRelayerClient();
  const artifact = loadArtifact();

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: relayerWallet },
  });

  // Relayer is contract owner / arbitrator
  const txHash = await contract.write.resolveDispute(
    [reservationId, resolutionCode, resolutionEvidence],
    { account: relayerAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const resolveLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'ReservationDisputeResolved',
  });

  if (resolveLogs.length === 0) {
    throw new Error('No ReservationDisputeResolved event found in transaction receipt logs.');
  }

  jsonOut({
    success: true,
    action: 'resolve-dispute',
    reservationId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    resolver: relayerAccount.address,
    outcome: STATUS_NAMES[resolutionCode],
    outcomeCode: resolutionCode,
    resolutionEvidence,
    status: STATUS_NAMES[resolutionCode],
  });
}

async function cmdGetReservation(args) {
  const reservationId = args.reservationId;
  if (!reservationId || !isHex(reservationId) || reservationId.length !== 66) {
    throw new Error('Missing or invalid reservationId.');
  }

  const client = getPublicClient();
  const artifact = loadArtifact();
  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client,
  });

  const reservationExists = await contract.read.exists([reservationId]);
  if (!reservationExists) {
    jsonOut({
      success: true,
      exists: false,
      reservationId,
      message: 'Reservation not found on Coral Testnet.',
    });
    return;
  }

  const res = await contract.read.reservations([reservationId]);
  const statusCode = Number(res[5]);

  jsonOut({
    success: true,
    exists: true,
    reservation: {
      id: res[0],
      resourceRef: res[1],
      requester: res[2],
      provider: res[3],
      expiresAt: res[4].toString(),
      status: STATUS_NAMES[statusCode] || 'Unknown',
      statusCode,
      createdAt: res[6].toString(),
      fulfilledAt: res[7].toString(),
      disputeEvidence: res[8],
      disputedBy: res[9],
    },
  });
}

async function cmdCheckInstitution(args) {
  const target = args.wallet || args.institution || args.address || 'hospital-a';
  let addr;
  try {
    addr = resolveAddress(target);
  } catch {
    addr = target;
  }
  const client = getPublicClient();
  const artifact = loadArtifact();
  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client,
  });

  const [info, isRequester, isProvider] = await Promise.all([
    contract.read.getInstitutionByWallet([addr]),
    contract.read.isRequesterWallet([addr]),
    contract.read.isProviderWallet([addr]),
  ]);

  const roleNames = ['None', 'Requester', 'Provider', 'Both'];
  jsonOut({
    success: true,
    address: addr,
    institutionId: info[0],
    name: info[1],
    role: roleNames[info[2]] || 'Unknown',
    roleCode: info[2],
    active: info[3],
    isRequesterWallet: isRequester,
    isProviderWallet: isProvider,
  });
}

function formatResourceType(resourceTypeRaw) {
  if (!resourceTypeRaw) {
    resourceTypeRaw = 'diagnostic_slot';
  }
  if (isHex(resourceTypeRaw) && resourceTypeRaw.length === 66) {
    return resourceTypeRaw;
  }
  return pad(stringToHex(resourceTypeRaw), { size: 32 });
}

// -------------------------------------------------------------------
// Feature 3: Capacity Management & Autonomous Locking Commands
// -------------------------------------------------------------------

async function cmdSetInventory(args) {
  const providerId = args.provider || 'lab-c';
  const providerAccount = resolveAccount(providerId);
  const resourceType = formatResourceType(args.resourceType);
  const totalCapacity = parseInt(args.totalCapacity || args.capacity || '12', 10);
  const isActive = args.isActive !== undefined ? (args.isActive === 'true' || args.isActive === true) : true;

  const client = getPublicClient();
  const artifact = loadArtifact();

  // 1. Ensure provider has sufficient gas
  const topUpTxHash = await ensureGasWithLock(providerAccount.address, client);

  // 2. Provider submits transaction directly
  const providerWallet = createWalletClient({
    account: providerAccount,
    chain: coralChain,
    transport: http(RPC_URL),
  });

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: providerWallet },
  });

  const txHash = await contract.write.setResourceInventory(
    [resourceType, totalCapacity, isActive],
    { account: providerAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const setLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'ResourceInventorySet',
  });

  jsonOut({
    success: true,
    action: 'set-inventory',
    provider: providerAccount.address,
    resourceTypeHex: resourceType,
    resourceType: args.resourceType || 'diagnostic_slot',
    totalCapacity,
    isActive,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    relayerTopUpTx: topUpTxHash,
  });
}

async function cmdGetCapacity(args) {
  const providerTarget = args.provider || 'lab-c';
  const providerAddress = resolveAddress(providerTarget);
  const resourceType = formatResourceType(args.resourceType);

  const client = getPublicClient();
  const artifact = loadArtifact();
  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client,
  });

  const [available, inv] = await Promise.all([
    contract.read.getAvailableCapacity([providerAddress, resourceType]),
    contract.read.getResourceInventory([providerAddress, resourceType]),
  ]);

  jsonOut({
    success: true,
    provider: providerAddress,
    resourceType: args.resourceType || 'diagnostic_slot',
    resourceTypeHex: resourceType,
    totalCapacity: Number(inv[0]),
    lockedCapacity: Number(inv[1]),
    committedCapacity: Number(inv[2]),
    availableCapacity: Number(available),
    isActive: inv[4],
  });
}

async function cmdLockCapacity(args) {
  const requesterId = args.requester || 'hospital-a';
  const providerId = args.provider || 'lab-c';
  const resourceTypeStr = args.resourceType || 'diagnostic_slot';
  const resourceType = formatResourceType(resourceTypeStr);
  const quantity = parseInt(args.quantity || '1', 10);
  const duration = BigInt(args.duration || 1800); // 30 minutes default

  const requesterAccount = resolveAccount(requesterId);
  const providerAddress = resolveAddress(providerId);

  const client = getPublicClient();
  const { account: relayerAccount, client: walletClient } = getRelayerClient();
  const artifact = loadArtifact();

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: walletClient },
  });

  const latestBlock = await client.getBlock();
  const deadline = latestBlock.timestamp + 1800n; // 30 mins to relay
  const nonce = await contract.read.nonces([requesterAccount.address]);

  const domain = {
    name: 'MEDwayReservation',
    version: '1',
    chainId: CHAIN_ID,
    verifyingContract: CONTRACT_ADDRESS,
  };

  const types = {
    CapacityLockRequest: [
      { name: 'requester', type: 'address' },
      { name: 'provider', type: 'address' },
      { name: 'resourceType', type: 'bytes32' },
      { name: 'quantity', type: 'uint32' },
      { name: 'duration', type: 'uint64' },
      { name: 'nonce', type: 'uint256' },
      { name: 'deadline', type: 'uint256' },
    ],
  };

  // 1. Off-chain EIP-712 typed signature by requester
  const signature = await requesterAccount.signTypedData({
    domain,
    types,
    primaryType: 'CapacityLockRequest',
    message: {
      requester: requesterAccount.address,
      provider: providerAddress,
      resourceType,
      quantity,
      duration,
      nonce,
      deadline,
    },
  });

  // 2. Relayer submits gas-sponsored lockCapacityWithSig transaction
  const txHash = await contract.write.lockCapacityWithSig(
    [
      requesterAccount.address,
      providerAddress,
      resourceType,
      quantity,
      duration,
      deadline,
      signature,
    ],
    { account: relayerAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const lockLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'CapacityLocked',
  });

  if (lockLogs.length === 0) {
    throw new Error('No CapacityLocked event found in transaction receipt logs.');
  }

  const lockId = lockLogs[0].args.lockId;
  const expiresAt = lockLogs[0].args.expiresAt;

  jsonOut({
    success: true,
    action: 'lock-capacity',
    lockId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    requester: requesterAccount.address,
    provider: providerAddress,
    resourceType: resourceTypeStr,
    quantity,
    duration: Number(duration),
    expiresAt: expiresAt.toString(),
    status: 'Active',
    relayer: relayerAccount.address,
  });
}

async function cmdCreateReservationWithLock(args) {
  const lockId = args.lockId;
  if (!lockId || !isHex(lockId) || lockId.length !== 66) {
    throw new Error('Missing or invalid lockId (must be 0x... 32-byte hex).');
  }

  const resourceRefRaw = args.resourceRef || `slot-lock-${Date.now()}`;
  const expiresIn = BigInt(args.expiresIn || 7200);

  const client = getPublicClient();
  const { account: relayerAccount, client: walletClient } = getRelayerClient();
  const artifact = loadArtifact();

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: walletClient },
  });

  const latestBlock = await client.getBlock();
  const expiresAt = latestBlock.timestamp + expiresIn;

  const resourceRef = resourceRefRaw.startsWith('0x') && resourceRefRaw.length === 66
    ? resourceRefRaw
    : pad(stringToHex(resourceRefRaw), { size: 32 });

  // Relayer (contract owner) converts the lock atomically
  const txHash = await contract.write.createReservationWithLock(
    [lockId, resourceRef, expiresAt],
    { account: relayerAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const createdLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'ReservationCreated',
  });

  if (createdLogs.length === 0) {
    throw new Error('No ReservationCreated event found in transaction receipt logs.');
  }

  const reservationId = createdLogs[0].args.reservationId;

  const convertLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'CapacityLockConverted',
  });

  jsonOut({
    success: true,
    action: 'create-reservation-with-lock',
    reservationId,
    lockId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    resourceRef,
    expiresAt: expiresAt.toString(),
    status: 'Proposed',
    relayer: relayerAccount.address,
    convertedQuantity: convertLogs.length > 0 ? Number(convertLogs[0].args.quantity) : null,
  });
}

async function cmdReleaseLock(args) {
  const lockId = args.lockId;
  if (!lockId || !isHex(lockId) || lockId.length !== 66) {
    throw new Error('Missing or invalid lockId (must be 0x... 32-byte hex).');
  }

  const reason = args.reason || 'Manual release via coordinator';

  const client = getPublicClient();
  const { account: relayerAccount, client: walletClient } = getRelayerClient();
  const artifact = loadArtifact();

  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: client, wallet: walletClient },
  });

  // Relayer (owner/keeper) executes releaseLock
  const txHash = await contract.write.releaseLock(
    [lockId, reason],
    { account: relayerAccount }
  );

  const receipt = await client.waitForTransactionReceipt({ hash: txHash });

  const releaseLogs = parseEventLogs({
    abi: artifact.abi,
    logs: receipt.logs,
    eventName: 'CapacityLockReleased',
  });

  if (releaseLogs.length === 0) {
    throw new Error('No CapacityLockReleased event found in transaction receipt logs.');
  }

  jsonOut({
    success: true,
    action: 'release-lock',
    lockId,
    txHash,
    blockNumber: Number(receipt.blockNumber),
    gasUsed: receipt.gasUsed.toString(),
    releasedBy: relayerAccount.address,
    releasedQuantity: Number(releaseLogs[0].args.quantity),
    reason,
    status: 'Released',
  });
}

async function cmdGetLock(args) {
  const lockId = args.lockId;
  if (!lockId || !isHex(lockId) || lockId.length !== 66) {
    throw new Error('Missing or invalid lockId (must be 0x... 32-byte hex).');
  }

  const client = getPublicClient();
  const artifact = loadArtifact();
  const contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client,
  });

  const exists = await contract.read.lockExists([lockId]);
  if (!exists) {
    jsonOut({
      success: true,
      exists: false,
      lockId,
      message: 'Capacity lock not found on-chain.',
    });
    return;
  }

  const lock = await contract.read.capacityLocks([lockId]);
  const lockStatusNames = ['None', 'Active', 'Converted', 'Released'];
  const statusCode = Number(lock[7]);

  jsonOut({
    success: true,
    exists: true,
    lock: {
      lockId: lock[0],
      resourceType: lock[1],
      provider: lock[2],
      requester: lock[3],
      quantity: Number(lock[4]),
      createdAt: lock[5].toString(),
      expiresAt: lock[6].toString(),
      status: lockStatusNames[statusCode] || 'Unknown',
      statusCode,
      reservationId: lock[8],
    },
  });
}

// -------------------------------------------------------------------
// CLI Argument Parsing
// -------------------------------------------------------------------

function parseArgs(rawArgs) {
  const args = {};
  for (let i = 0; i < rawArgs.length; i++) {
    const arg = rawArgs[i];
    if (arg.startsWith('--')) {
      const key = arg.slice(2);
      const next = rawArgs[i + 1];
      if (next && !next.startsWith('--')) {
        args[key] = next;
        i++;
      } else {
        args[key] = true;
      }
    } else if (!args.command) {
      args.command = arg;
    }
  }
  return args;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args.command || 'status';

  switch (command) {
    case 'status':
    case 'health':
      await cmdStatus();
      break;
    case 'create':
    case 'create-reservation':
      await cmdCreateReservation(args);
      break;
    case 'accept':
    case 'accept-reservation':
      await cmdAcceptReservation(args);
      break;
    case 'get':
    case 'get-reservation':
      await cmdGetReservation(args);
      break;
    case 'fulfill':
    case 'fulfill-reservation':
      await cmdFulfillReservation(args);
      break;
    case 'dispute':
    case 'dispute-reservation':
      await cmdDisputeReservation(args);
      break;
    case 'resolve':
    case 'resolve-dispute':
      await cmdResolveDispute(args);
      break;
    case 'check-institution':
    case 'institution':
      await cmdCheckInstitution(args);
      break;
    case 'set-inventory':
    case 'inventory':
      await cmdSetInventory(args);
      break;
    case 'get-capacity':
    case 'available-capacity':
    case 'capacity':
      await cmdGetCapacity(args);
      break;
    case 'lock':
    case 'lock-capacity':
      await cmdLockCapacity(args);
      break;
    case 'create-reservation-with-lock':
    case 'convert-lock':
      await cmdCreateReservationWithLock(args);
      break;
    case 'release-lock':
    case 'unlock':
      await cmdReleaseLock(args);
      break;
    case 'get-lock':
    case 'lock-status':
      await cmdGetLock(args);
      break;
    default:
      throw new Error(`Unknown command '${command}'. Use status, create, accept, get, fulfill, dispute, resolve, check-institution, set-inventory, get-capacity, lock-capacity, create-reservation-with-lock, release-lock, or get-lock.`);
  }
}

main().catch((err) => {
  jsonOut({
    success: false,
    error: err.message || String(err),
  });
  process.exit(1);
});
