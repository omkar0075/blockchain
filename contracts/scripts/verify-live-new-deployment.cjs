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
  parseEventLogs,
} = require('viem');
const { privateKeyToAccount, generatePrivateKey } = require('viem/accounts');

// Load environment
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
      if (!process.env[key]) process.env[key] = val;
    }
  }
}

const RPC_URL = process.env.CORAL_RPC_URL || 'https://coralrpc.mstblockchain.com';
const CHAIN_ID = 54635;
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS || '0xbafa85b0797d0a793a2c934d623f02588fe9770a';
const RELAYER_KEY = process.env.CORAL_PRIVATE_KEY;
const HOSP_A_KEY = process.env.HOSPITAL_A_PRIVATE_KEY;
const LAB_C_KEY = process.env.LAB_C_PRIVATE_KEY;

const coralChain = defineChain({
  id: CHAIN_ID,
  name: 'Coral Testnet',
  nativeCurrency: { name: 'MST', symbol: 'MST', decimals: 18 },
  rpcUrls: { default: { http: [RPC_URL] } },
});

const transport = http(RPC_URL, { timeout: 60_000, retryCount: 3 });
const publicClient = createPublicClient({ chain: coralChain, transport });

const relayerAccount = privateKeyToAccount(RELAYER_KEY);
const hospitalAAccount = privateKeyToAccount(HOSP_A_KEY);
const labCAccount = privateKeyToAccount(LAB_C_KEY);

const relayerWallet = createWalletClient({ account: relayerAccount, chain: coralChain, transport });

const artifactPath = path.resolve(__dirname, '../artifacts/contracts/MEDwayReservation.sol/MEDwayReservation.json');
const artifact = JSON.parse(fs.readFileSync(artifactPath, 'utf8'));

const reservationContract = getContract({
  address: CONTRACT_ADDRESS,
  abi: artifact.abi,
  client: { public: publicClient, wallet: relayerWallet },
});

const reservationRequestTypes = {
  ReservationRequest: [
    { name: 'requester', type: 'address' },
    { name: 'provider', type: 'address' },
    { name: 'resourceRef', type: 'bytes32' },
    { name: 'expiresAt', type: 'uint64' },
    { name: 'nonce', type: 'uint256' },
    { name: 'deadline', type: 'uint256' },
  ],
};

const acceptRequestTypes = {
  AcceptRequest: [
    { name: 'reservationId', type: 'bytes32' },
    { name: 'provider', type: 'address' },
    { name: 'nonce', type: 'uint256' },
    { name: 'deadline', type: 'uint256' },
  ],
};

async function runLiveVerification() {
  console.log('='.repeat(72));
  console.log(' LIVE CORAL TESTNET ADVERSARIAL & GOVERNANCE VERIFICATION');
  console.log('='.repeat(72));
  console.log(`[*] Target Contract: ${CONTRACT_ADDRESS}`);
  console.log(`[*] Network RPC:     ${RPC_URL} (Chain ID: ${CHAIN_ID})`);
  console.log(`[*] Relayer:         ${relayerAccount.address}`);
  console.log(`[*] Hospital A:      ${hospitalAAccount.address}`);
  console.log(`[*] Lab C:           ${labCAccount.address}\n`);

  const domain = {
    name: 'MEDwayReservation',
    version: '1',
    chainId: CHAIN_ID,
    verifyingContract: CONTRACT_ADDRESS,
  };

  // -------------------------------------------------------------------
  // 1. Verify On-Chain Registry
  // -------------------------------------------------------------------
  console.log('--- TEST 1: Institutional Identity Registry Checks ---');
  const [hospAId, hospAName, hospARole, hospAActive, hospAWalletActive] =
    await reservationContract.read.getInstitutionByWallet([hospitalAAccount.address]);
  console.log(`  [+] Hospital A Registered: ${hospAName} | Active=${hospAActive && hospAWalletActive} | Role=${hospARole}`);
  if (!hospAActive || !hospAWalletActive) throw new Error('Hospital A is not active in registry!');

  const [labCId, labCName, labCRole, labCActive, labCWalletActive] =
    await reservationContract.read.getInstitutionByWallet([labCAccount.address]);
  console.log(`  [+] Lab C Registered:      ${labCName} | Active=${labCActive && labCWalletActive} | Role=${labCRole}`);
  if (!labCActive || !labCWalletActive) throw new Error('Lab C is not active in registry!');

  // -------------------------------------------------------------------
  // 2. EIP-712 Gasless Creation
  // -------------------------------------------------------------------
  console.log('\n--- TEST 2: EIP-712 Gasless Reservation Proposal ---');
  const latestBlock = await publicClient.getBlock();
  const expiresAt = latestBlock.timestamp + 7200n;
  const deadline = latestBlock.timestamp + 3600n;
  const resourceRef = pad(stringToHex(`mri-slot-live-${Date.now()}`), { size: 32 });

  const hospANonce = await reservationContract.read.nonces([hospitalAAccount.address]);
  const reqSig = await hospitalAAccount.signTypedData({
    domain,
    types: reservationRequestTypes,
    primaryType: 'ReservationRequest',
    message: {
      requester: hospitalAAccount.address,
      provider: labCAccount.address,
      resourceRef,
      expiresAt,
      nonce: hospANonce,
      deadline,
    },
  });
  console.log(`  [+] Hospital A signed EIP-712 proposal off-chain (Nonce: ${hospANonce})`);

  const createTx = await reservationContract.write.createReservationWithSig([
    hospitalAAccount.address,
    labCAccount.address,
    resourceRef,
    expiresAt,
    deadline,
    reqSig,
  ]);
  const createReceipt = await publicClient.waitForTransactionReceipt({ hash: createTx });
  const createLogs = parseEventLogs({
    abi: artifact.abi,
    logs: createReceipt.logs,
    eventName: 'ReservationCreated',
  });
  const reservationId = createLogs[0].args.reservationId;
  console.log(`  [✓] Relayer submitted tx on Coral. Block #${createReceipt.blockNumber}`);
  console.log(`  [✓] Created Reservation ID: ${reservationId}`);

  // -------------------------------------------------------------------
  // 3. EIP-712 Gasless Acceptance
  // -------------------------------------------------------------------
  console.log('\n--- TEST 3: EIP-712 Gasless Reservation Acceptance ---');
  const labCNonce = await reservationContract.read.nonces([labCAccount.address]);
  const acceptSig = await labCAccount.signTypedData({
    domain,
    types: acceptRequestTypes,
    primaryType: 'AcceptRequest',
    message: {
      reservationId,
      provider: labCAccount.address,
      nonce: labCNonce,
      deadline,
    },
  });
  console.log(`  [+] Lab C signed EIP-712 acceptance off-chain (Nonce: ${labCNonce})`);

  const acceptTx = await reservationContract.write.acceptReservationWithSig([
    reservationId,
    deadline,
    acceptSig,
  ]);
  const acceptReceipt = await publicClient.waitForTransactionReceipt({ hash: acceptTx });
  console.log(`  [✓] Relayer committed reservation on Coral. Block #${acceptReceipt.blockNumber} (Status: Committed)`);

  // -------------------------------------------------------------------
  // 4. Wallet Rotation & Fulfillment Safety (Items A & B)
  // -------------------------------------------------------------------
  console.log('\n--- TEST 4: Authorized Wallet Rotation & Fulfillment ---');
  // Generate a new operational key for Lab C
  const labCAltAccount = privateKeyToAccount(generatePrivateKey());
  console.log(`  [*] Authorizing secondary wallet for Lab C: ${labCAltAccount.address}`);
  const authTx = await reservationContract.write.authorizeWallet([labCId, labCAltAccount.address]);
  await publicClient.waitForTransactionReceipt({ hash: authTx });

  console.log(`  [*] Rotating active reservation provider from primary to secondary wallet...`);
  const rotateTx = await reservationContract.write.rotateReservationWallet([
    reservationId,
    labCAltAccount.address,
  ]);
  const rotateReceipt = await publicClient.waitForTransactionReceipt({ hash: rotateTx });
  const rotateLogs = parseEventLogs({
    abi: artifact.abi,
    logs: rotateReceipt.logs,
    eventName: 'ReservationWalletRotated',
  });
  console.log(`  [✓] Confirmed in Block #${rotateReceipt.blockNumber}!`);
  console.log(`      Old Wallet: ${rotateLogs[0].args.oldWallet}`);
  console.log(`      New Wallet: ${rotateLogs[0].args.newWallet}`);

  // Verify reservation data on-chain
  const resOnChain = await reservationContract.read.reservations([reservationId]);
  if (resOnChain[3].toLowerCase() !== labCAltAccount.address.toLowerCase()) {
    throw new Error('On-chain reservation provider was not updated!');
  }
  if (resOnChain[1] !== resourceRef) {
    throw new Error('On-chain resourceRef was altered!');
  }
  console.log(`  [✓] Provider updated to secondary wallet. resourceRef remains 100% UNCHANGED.`);

  // Fulfill reservation using the rotated wallet
  console.log(`  [*] Funding rotated secondary wallet (${labCAltAccount.address}) with 0.05 MST for gas...`);
  const fundLabCTx = await relayerWallet.sendTransaction({
    to: labCAltAccount.address,
    value: 50000000000000000n, // 0.05 MST
  });
  await publicClient.waitForTransactionReceipt({ hash: fundLabCTx });

  console.log(`  [*] Fulfilling reservation using rotated secondary wallet...`);
  const altWalletClient = createWalletClient({ account: labCAltAccount, chain: coralChain, transport });
  const altContract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: publicClient, wallet: altWalletClient },
  });
  const fulfillTx = await altContract.write.fulfillReservation([reservationId]);
  const fulfillReceipt = await publicClient.waitForTransactionReceipt({ hash: fulfillTx });
  console.log(`  [✓] Reservation Fulfilled by rotated wallet in Block #${fulfillReceipt.blockNumber}!`);

  // -------------------------------------------------------------------
  // 5. Consortium Arbitration Board & Multi-Arbitrator Voting (Item D)
  // -------------------------------------------------------------------
  console.log('\n--- TEST 5: Consortium Arbitration Board (Multi-Party Quorum) ---');
  const arbitratorAccount = privateKeyToAccount(generatePrivateKey());
  console.log(`  [*] Appointing new consortium arbitrator: ${arbitratorAccount.address}`);
  const addArbTx = await reservationContract.write.addArbitrator([arbitratorAccount.address]);
  await publicClient.waitForTransactionReceipt({ hash: addArbTx });

  console.log(`  [*] Setting arbitration quorum to 2-of-2...`);
  const setQuorumTx = await reservationContract.write.setArbitrationQuorum([2n]);
  await publicClient.waitForTransactionReceipt({ hash: setQuorumTx });

  const arbCount = await reservationContract.read.arbitratorCount();
  const quorum = await reservationContract.read.requiredArbitrationQuorum();
  console.log(`  [+] Consortium Board: ${arbCount} Arbitrators | Required Quorum: ${quorum}`);

  // Create & commit a second reservation to dispute
  console.log(`  [*] Creating reservation #2 for dispute resolution test...`);
  const res2ResourceRef = pad(stringToHex(`dispute-test-${Date.now()}`), { size: 32 });
  const res2ExpiresAt = (await publicClient.getBlock()).timestamp + 7200n;
  const res2Deadline = (await publicClient.getBlock()).timestamp + 3600n;
  const h2Nonce = await reservationContract.read.nonces([hospitalAAccount.address]);
  const reqSig2 = await hospitalAAccount.signTypedData({
    domain,
    types: reservationRequestTypes,
    primaryType: 'ReservationRequest',
    message: {
      requester: hospitalAAccount.address,
      provider: labCAccount.address,
      resourceRef: res2ResourceRef,
      expiresAt: res2ExpiresAt,
      nonce: h2Nonce,
      deadline: res2Deadline,
    },
  });
  const c2Tx = await reservationContract.write.createReservationWithSig([
    hospitalAAccount.address,
    labCAccount.address,
    res2ResourceRef,
    res2ExpiresAt,
    res2Deadline,
    reqSig2,
  ]);
  const c2Receipt = await publicClient.waitForTransactionReceipt({ hash: c2Tx });
  const res2Id = parseEventLogs({ abi: artifact.abi, logs: c2Receipt.logs, eventName: 'ReservationCreated' })[0].args.reservationId;

  const l2Nonce = await reservationContract.read.nonces([labCAccount.address]);
  const accSig2 = await labCAccount.signTypedData({
    domain,
    types: acceptRequestTypes,
    primaryType: 'AcceptRequest',
    message: { reservationId: res2Id, provider: labCAccount.address, nonce: l2Nonce, deadline: res2Deadline },
  });
  const a2Tx = await reservationContract.write.acceptReservationWithSig([res2Id, res2Deadline, accSig2]);
  await publicClient.waitForTransactionReceipt({ hash: a2Tx });

  // Raise dispute (must be called by participating requester or provider)
  const evidenceHash = pad(stringToHex('clinical-delivery-dispute-memo'), { size: 32 });
  console.log(`  [*] Funding Hospital A with 0.05 MST to submit dispute transaction...`);
  const fundHospATx = await relayerWallet.sendTransaction({
    to: hospitalAAccount.address,
    value: 50000000000000000n, // 0.05 MST
  });
  await publicClient.waitForTransactionReceipt({ hash: fundHospATx });

  const hospAWallet = createWalletClient({ account: hospitalAAccount, chain: coralChain, transport });
  const hospAContract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: publicClient, wallet: hospAWallet },
  });

  const disputeTx = await hospAContract.write.disputeReservation([res2Id, evidenceHash]);
  await publicClient.waitForTransactionReceipt({ hash: disputeTx });
  console.log(`  [✓] Reservation #2 disputed by Hospital A with evidence hash.`);

  // Arbitrator 1 (Deployer) casts vote 1
  const rulingEvidence = pad(stringToHex('consortium-board-ruling-001'), { size: 32 });
  console.log(`  [*] Arbitrator 1 (Owner) casts vote for CANCELLED...`);
  const vote1Tx = await reservationContract.write.castArbitrationVote([res2Id, 3, rulingEvidence]); // 3 = Cancelled
  await publicClient.waitForTransactionReceipt({ hash: vote1Tx });

  let rulingState = await reservationContract.read.getDisputeRuling([res2Id]);
  console.log(`  [+] Vote 1 recorded: CancelledVotes=${rulingState[1]} | Resolved=${rulingState[2]}`);
  if (rulingState[2]) throw new Error('Dispute resolved prematurely before quorum!');

  // Fund arbitratorAccount slightly for gas to cast vote
  console.log(`  [*] Funding Arbitrator 2 with 0.05 MST for voting...`);
  const fundTx = await relayerWallet.sendTransaction({
    to: arbitratorAccount.address,
    value: 50000000000000000n, // 0.05 MST
  });
  await publicClient.waitForTransactionReceipt({ hash: fundTx });

  // Arbitrator 2 casts vote 2 -> Reaches Quorum (2)
  console.log(`  [*] Arbitrator 2 casts vote for CANCELLED (reaching quorum 2-of-2)...`);
  const arb2Wallet = createWalletClient({ account: arbitratorAccount, chain: coralChain, transport });
  const arb2Contract = getContract({
    address: CONTRACT_ADDRESS,
    abi: artifact.abi,
    client: { public: publicClient, wallet: arb2Wallet },
  });
  const vote2Tx = await arb2Contract.write.castArbitrationVote([res2Id, 3, rulingEvidence]);
  const vote2Receipt = await publicClient.waitForTransactionReceipt({ hash: vote2Tx });
  const resolvedLogs = parseEventLogs({
    abi: artifact.abi,
    logs: vote2Receipt.logs,
    eventName: 'ReservationDisputeResolved',
  });
  console.log(`  [✓] DISPUTE AUTO-FINALIZED in Block #${vote2Receipt.blockNumber}!`);
  console.log(`      Outcome: Cancelled (Status Code 3)`);
  console.log(`      Final Resolver: ${resolvedLogs[0].args.resolver}`);

  const res2Final = await reservationContract.read.reservations([res2Id]);
  if (res2Final[5] !== 3) throw new Error('Reservation status was not updated to Cancelled!');
  console.log(`  [✓] Verified on-chain status = Cancelled (Code 3).`);

  // Reset quorum back to 1 for seamless testnet continuity
  console.log(`  [*] Re-adjusting quorum to 1 for continuous testnet integration...`);
  const resetQuorumTx = await reservationContract.write.setArbitrationQuorum([1n]);
  await publicClient.waitForTransactionReceipt({ hash: resetQuorumTx });

  console.log('\n' + '='.repeat(72));
  console.log(' ALL LIVE CORAL TESTNET ADVERSARIAL & GOVERNANCE TESTS PASSED!');
  console.log('='.repeat(72));
}

runLiveVerification().catch((err) => {
  console.error('\n[!] Live verification failed:', err);
  process.exit(1);
});
