const fs = require('fs');
const path = require('path');

// 1. Read .env file if available
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

async function rpcCall(method, params = []) {
  const response = await fetch(rpcUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method,
      params,
    }),
  });

  if (!response.ok) {
    throw new Error(`HTTP Error ${response.status}: ${response.statusText}`);
  }

  const json = await response.json();
  if (json.error) {
    throw new Error(`RPC Error: ${json.error.message || JSON.stringify(json.error)}`);
  }
  return json.result;
}

async function main() {
  console.log('Testing read-only connection from backend to Coral Testnet...');
  console.log(`RPC Endpoint:     ${rpcUrl}`);
  console.log(`Contract Address: ${contractAddress}\n`);

  // 1. Check Chain ID
  console.log('Querying eth_chainId...');
  const chainIdHex = await rpcCall('eth_chainId');
  const chainId = parseInt(chainIdHex, 16);
  console.log(`  Chain ID: ${chainId} (hex: ${chainIdHex})`);

  // 2. Check Latest Block Number
  console.log('Querying eth_blockNumber...');
  const blockNumHex = await rpcCall('eth_blockNumber');
  const blockNumber = parseInt(blockNumHex, 16);
  console.log(`  Latest Block: #${blockNumber}`);

  // 3. Read Contract Bytecode
  console.log('Querying eth_getCode at contract address...');
  const bytecode = await rpcCall('eth_getCode', [contractAddress, 'latest']);

  if (!bytecode || bytecode === '0x') {
    throw new Error(
      `No contract bytecode found at ${contractAddress}. Check if the address is correct.`
    );
  }

  const byteLength = (bytecode.length - 2) / 2;

  console.log('\n========================================================');
  console.log(' Coral Testnet Backend Connection Verified!');
  console.log('========================================================');
  console.log(` Network:          Coral Testnet (Chain ID: ${chainId})`);
  console.log(` Latest Block:     #${blockNumber}`);
  console.log(` Contract Address: ${contractAddress}`);
  console.log(` Bytecode Length:  ${byteLength} bytes`);
  console.log(` Status:           LIVE ON-CHAIN & READABLE`);
  console.log(' Cost:             0 MST tokens (Read-only call)');
  console.log('========================================================\n');
}

main().catch((error) => {
  console.error('\n[FAILED] Coral read-only check failed:', error.message || error);
  process.exit(1);
});

