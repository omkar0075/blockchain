const fs = require('fs');
const path = require('path');
const { createPublicClient, http, getContract } = require('../contracts/node_modules/viem');

// 1. Read configuration from backend/.env
const envPath = path.join(__dirname, '.env');
let rpcUrl = 'https://coralrpc.mstblockchain.com';
let contractAddress = '0x8e8f0fa3541f6599affcbdeab71ce4fd5b2bf06d';

if (fs.existsSync(envPath)) {
  const envContent = fs.readFileSync(envPath, 'utf8');
  for (const line of envContent.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith('#')) {
      const [key, ...values] = trimmed.split('=');
      const val = values.join('=').trim();
      if (key === 'CORAL_RPC_URL') rpcUrl = val;
      if (key === 'CONTRACT_ADDRESS' || key === 'MEDWAY_CONTRACT_ADDRESS') contractAddress = val;
    }
  }
}

// 2. Load ABI directly from compiled Hardhat artifact
const artifactPath = path.join(
  __dirname,
  '../contracts/artifacts/contracts/MEDwayReservation.sol/MEDwayReservation.json'
);

if (!fs.existsSync(artifactPath)) {
  console.error(`Artifact not found at ${artifactPath}. Please run 'npx hardhat build' in contracts.`);
  process.exit(1);
}

const artifact = JSON.parse(fs.readFileSync(artifactPath, 'utf8'));

async function main() {
  console.log('Connecting to Coral Testnet from Backend...');
  console.log(`RPC Endpoint:     ${rpcUrl}`);
  console.log(`Contract Address: ${contractAddress}`);
  console.log(`Artifact Loaded:  ${artifact.contractName} (${artifact.abi.length} ABI entries)\n`);

  // 3. Create Viem public client
  const client = createPublicClient({
    transport: http(rpcUrl),
  });

  // 4. Instantiate typed contract interface using artifact ABI
  const reservation = getContract({
    address: contractAddress,
    abi: artifact.abi,
    client,
  });

  console.log('Querying on-chain state functions...');
  const [owner, disputeWindow, blockNumber] = await Promise.all([
    reservation.read.owner(),
    reservation.read.DISPUTE_WINDOW(),
    client.getBlockNumber(),
  ]);

  const disputeDays = Number(disputeWindow) / 86400;

  // 5. Test if EIP-712 upgrade is active on this deployment
  let hasEip712 = false;
  let domainSeparator = 'Not Available';
  try {
    domainSeparator = await reservation.read.DOMAIN_SEPARATOR();
    hasEip712 = true;
  } catch {
    hasEip712 = false;
  }

  console.log('\n========================================================');
  console.log(' Coral Contract State Verification');
  console.log('========================================================');
  console.log(` Network:          Coral Testnet`);
  console.log(` Latest Block:     #${blockNumber}`);
  console.log(` Contract Address: ${contractAddress}`);
  console.log(` Contract Owner:   ${owner}`);
  console.log(` Dispute Window:   ${disputeWindow} seconds (${disputeDays} days)`);
  console.log('--------------------------------------------------------');
  if (hasEip712) {
    console.log(' EIP-712 Support:  ACTIVE (Upgraded Build with Gasless Meta-Tx)');
    console.log(` Domain Separator: ${domainSeparator}`);
  } else {
    console.log(' EIP-712 Support:  NOT SUPPORTED (This is the older build)');
    console.log(' Status:           Needs upgraded deployment to support gasless relaying');
  }
  console.log('========================================================\n');
}

main().catch((error) => {
  console.error('\n[FAILED] Contract state verification failed:', error?.message || error);
  process.exit(1);
});
