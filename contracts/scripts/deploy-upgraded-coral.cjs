const fs = require('fs');
const path = require('path');
const {
  createPublicClient,
  createWalletClient,
  http,
  pad,
  stringToHex,
  getContract,
  defineChain,
} = require('viem');
const { privateKeyToAccount } = require('viem/accounts');

// Zero-dependency .env loader
function loadEnv() {
  const envPath = path.resolve(__dirname, '../../backend/.env');
  if (fs.existsSync(envPath)) {
    const lines = fs.readFileSync(envPath, 'utf8').split(/\r?\n/);
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
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
loadEnv();

const RPC_URL = process.env.CORAL_RPC_URL || 'https://coralrpc.mstblockchain.com';
const CHAIN_ID = 54635;
const PRIVATE_KEY = process.env.CORAL_PRIVATE_KEY;

if (!PRIVATE_KEY) {
  console.error('Error: CORAL_PRIVATE_KEY not set in backend/.env');
  process.exit(1);
}

const coralChain = defineChain({
  id: CHAIN_ID,
  name: 'Coral Testnet',
  nativeCurrency: { name: 'MST', symbol: 'MST', decimals: 18 },
  rpcUrls: { default: { http: [RPC_URL] } },
});

async function main() {
  console.log('='.repeat(70));
  console.log(' DEPLOYING UPGRADED MEDWAY CONTRACT WITH IDENTITY REGISTRY');
  console.log('='.repeat(70));

  const artifactPath = path.resolve(
    __dirname,
    '../artifacts/contracts/MEDwayReservation.sol/MEDwayReservation.json'
  );
  const artifact = JSON.parse(fs.readFileSync(artifactPath, 'utf8'));

  const account = privateKeyToAccount(PRIVATE_KEY);
  console.log(`[*] Deployer Account:   ${account.address}`);
  console.log(`[*] Network RPC:        ${RPC_URL} (Chain ID: ${CHAIN_ID})`);

  const transportConfig = http(RPC_URL, {
    timeout: 60_000,
    retryCount: 3,
    retryDelay: 2_000,
  });

  const publicClient = createPublicClient({
    chain: coralChain,
    transport: transportConfig,
  });

  const walletClient = createWalletClient({
    account,
    chain: coralChain,
    transport: transportConfig,
  });

  console.log('\n[*] Submitting contract deployment transaction...');
  const deployTxHash = await walletClient.deployContract({
    abi: artifact.abi,
    bytecode: artifact.bytecode,
    args: [],
  });
  console.log(`[+] Deployment Tx Hash: ${deployTxHash}`);

  console.log('[*] Waiting for deployment receipt...');
  const receipt = await publicClient.waitForTransactionReceipt({ hash: deployTxHash });
  const contractAddress = receipt.contractAddress;

  console.log(`\n======================================================`);
  console.log(` [✓] NEW CONTRACT DEPLOYED TO CORAL TESTNET!`);
  console.log(`     Address:      ${contractAddress}`);
  console.log(`     Block Number: #${receipt.blockNumber}`);
  console.log(`     Gas Used:     ${receipt.gasUsed.toString()}`);
  console.log(`======================================================\n`);

  // Initialize the contract instance
  const contract = getContract({
    address: contractAddress,
    abi: artifact.abi,
    client: { public: publicClient, wallet: walletClient },
  });

  // Roles: 1 = Requester, 2 = Provider, 3 = Both
  const InstitutionRole = {
    None: 0,
    Requester: 1,
    Provider: 2,
    Both: 3,
  };

  const institutionsToRegister = [
    {
      id: 'hospital-a',
      name: 'City Hospital A',
      role: InstitutionRole.Requester,
      wallet: process.env.HOSPITAL_A_ADDRESS || '0x4dB8336edB0af7FeD1311312081689993F80a18B',
    },
    {
      id: 'hospital-b',
      name: 'Community Hospital B',
      role: InstitutionRole.Requester,
      wallet: '0x70997970c51812e33987251372ecdbc2b3f3bb6e',
    },
    {
      id: 'lab-c',
      name: 'Central Diagnostic Lab C',
      role: InstitutionRole.Provider,
      wallet: process.env.LAB_C_ADDRESS || '0xb05049cc2A5a1c28979DEafc82B82749681DA92C',
    },
  ];

  console.log('[*] Registering initial accredited institutions in on-chain registry...');

  for (const inst of institutionsToRegister) {
    const instIdBytes = pad(stringToHex(inst.id), { size: 32 });
    console.log(`\n    -> Registering ${inst.name} (${inst.id})`);
    console.log(`       Wallet: ${inst.wallet} | Role: ${inst.role === 1 ? 'Requester' : 'Provider'}`);

    const tx = await contract.write.registerInstitution([
      instIdBytes,
      inst.name,
      inst.role,
      inst.wallet,
    ]);
    const regReceipt = await publicClient.waitForTransactionReceipt({ hash: tx });
    console.log(`       [✓] Confirmed in block #${regReceipt.blockNumber} (Tx: ${tx})`);
  }

  console.log('\n[*] Verifying on-chain registrations via getInstitutionByWallet...');
  for (const inst of institutionsToRegister) {
    const [instId, name, role, isInstActive, isWalletActive] = await contract.read.getInstitutionByWallet([
      inst.wallet,
    ]);
    console.log(`    - ${name}: Active=${isInstActive && isWalletActive} (Role Code: ${role})`);
  }

  console.log('\n' + '='.repeat(70));
  console.log(' DEPLOYMENT & ON-CHAIN REGISTRATION FULLY COMPLETED!');
  console.log(' New Contract Address:', contractAddress);
  console.log('='.repeat(70));
}

main().catch((err) => {
  console.error('Fatal error during deployment:', err);
  process.exit(1);
});
