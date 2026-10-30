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
  '0xFc71e5D5b2BD0648BCA20b66a09199E8D89BBEEC';

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
    default:
      throw new Error(`Unknown command '${command}'. Use status, create, accept, or get.`);
  }
}

main().catch((err) => {
  jsonOut({
    success: false,
    error: err.message || String(err),
  });
  process.exit(1);
});
