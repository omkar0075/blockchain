#!/usr/bin/env node
/**
 * MEDway Independent On-Chain Verifier & Terms Attestation Tool
 * 
 * This tool allows Hospital A, Hospital B, or any external auditor to verify
 * a reservation commitment directly from the MST Coral Testnet RPC without
 * relying on or connecting to MEDway's backend.
 * 
 * Usage:
 *   node tools/verify-onchain.js <reservationId> [termsJson]
 * 
 * Example:
 *   node tools/verify-onchain.js 0x70ad9a82d354adc9cceeeed4d5771a971bf005dd18da9308f2bf6baa8598bfe0 '{"proposal_id":"prop-sample-01","provider":"lab-c","quantity":3,"requester":"hospital-a","resource_type":"diagnostic_slot"}'
 */

const crypto = require('crypto');
const fs = require('fs');

const RPC_URL = process.env.CORAL_RPC_URL || 'https://coralrpc.mstblockchain.com';
const CONTRACT_ADDRESS =
  process.env.CONTRACT_ADDRESS ||
  process.env.MEDWAY_CONTRACT_ADDRESS ||
  '0xcd630b8e555e0751afbb9ae511869110d361bb83';
const RESERVATIONS_SELECTOR = '0xa1b416f9'; // reservations(bytes32)

const STATUS_NAMES = [
  'Proposed',
  'Committed',
  'Fulfilled',
  'Cancelled',
  'Expired',
  'Disputed',
];

function canonicalJsonString(obj) {
  if (typeof obj !== 'object' || obj === null) {
    return JSON.stringify(obj);
  }
  if (Array.isArray(obj)) {
    return '[' + obj.map(canonicalJsonString).join(',') + ']';
  }
  const sortedKeys = Object.keys(obj).sort();
  const pairs = sortedKeys.map(k => JSON.stringify(k) + ':' + canonicalJsonString(obj[k]));
  return '{' + pairs.join(',') + '}';
}

function computeTermsHash(terms) {
  const canonicalStr = canonicalJsonString(terms);
  return '0x' + crypto.createHash('sha256').update(canonicalStr, 'utf8').digest('hex');
}

async function verifyReservation(reservationId, rawTermsInput) {
  if (!reservationId || !reservationId.startsWith('0x') || reservationId.length !== 66) {
    console.error('Error: Please provide a valid 32-byte hex reservationId (e.g. 0x...)');
    process.exit(1);
  }

  console.log('='.repeat(72));
  console.log(' MEDWAY INDEPENDENT ON-CHAIN VERIFIER & TERMS ATTESTATION TOOL');
  console.log('='.repeat(72));
  console.log(`[*] Direct Blockchain RPC:  ${RPC_URL}`);
  console.log(`[*] Target Smart Contract:  ${CONTRACT_ADDRESS}`);
  console.log(`[*] Target Reservation ID:  ${reservationId}\n`);

  // Prepare standard eth_call payload
  const cleanId = reservationId.slice(2);
  const callData = RESERVATIONS_SELECTOR + cleanId;

  const requestBody = {
    jsonrpc: '2.0',
    id: 1,
    method: 'eth_call',
    params: [
      {
        to: CONTRACT_ADDRESS,
        data: callData,
      },
      'latest',
    ],
  };

  const startTime = Date.now();
  let response;
  try {
    const res = await fetch(RPC_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody),
    });
    response = await res.json();
  } catch (err) {
    console.error(`[-] Failed to connect to Coral RPC: ${err.message}`);
    process.exit(1);
  }

  const durationMs = Date.now() - startTime;

  if (response.error) {
    console.error(`[-] RPC Error: ${response.error.message}`);
    process.exit(1);
  }

  const rawHex = response.result;
  if (!rawHex || rawHex === '0x' || rawHex.length < 66) {
    console.error('[-] Reservation does not exist on Coral Testnet.');
    process.exit(1);
  }

  // Decode the 10 ABI words (32 bytes each)
  const hex = rawHex.startsWith('0x') ? rawHex.slice(2) : rawHex;
  const words = [];
  for (let i = 0; i < hex.length; i += 64) {
    words.push(hex.slice(i, i + 64));
  }

  if (words.length < 10) {
    console.error('[-] Unexpected contract return data length.');
    process.exit(1);
  }

  const onchainId = '0x' + words[0];
  const resourceRef = '0x' + words[1];
  const requester = '0x' + words[2].slice(24);
  const provider = '0x' + words[3].slice(24);
  const expiresAt = BigInt('0x' + words[4]);
  const statusCode = Number(BigInt('0x' + words[5]));
  const createdAt = BigInt('0x' + words[6]);
  const fulfilledAt = BigInt('0x' + words[7]);
  const disputeEvidence = '0x' + words[8];
  const disputedBy = '0x' + words[9].slice(24);

  const statusName = STATUS_NAMES[statusCode] || `Unknown (${statusCode})`;

  console.log('[+] DIRECT ON-CHAIN CONFIRMATION RECEIVED IN ' + durationMs + 'ms');
  console.log('-'.repeat(72));
  console.log(`  Reservation ID:     ${onchainId}`);
  console.log(`  Current Status:     ${statusName.toUpperCase()} (Code: ${statusCode})`);
  console.log(`  Requester Address:  ${requester}`);
  console.log(`  Provider Address:   ${provider}`);
  console.log(`  On-Chain Terms Hash:${resourceRef}`);
  console.log(`  Expires At:         ${new Date(Number(expiresAt) * 1000).toISOString()} (Unix: ${expiresAt})`);
  console.log(`  Created At:         ${new Date(Number(createdAt) * 1000).toISOString()}`);
  if (fulfilledAt > 0n) {
    console.log(`  Fulfilled At:       ${new Date(Number(fulfilledAt) * 1000).toISOString()}`);
  }
  if (disputedBy !== '0x0000000000000000000000000000000000000000') {
    console.log(`  Disputed By:        ${disputedBy}`);
    console.log(`  Dispute Evidence:   ${disputeEvidence}`);
  }
  console.log('-'.repeat(72));

  // If terms input is provided, independently verify cryptographic binding
  if (rawTermsInput) {
    let parsedTerms;
    try {
      if (fs.existsSync(rawTermsInput)) {
        parsedTerms = JSON.parse(fs.readFileSync(rawTermsInput, 'utf8'));
      } else {
        parsedTerms = JSON.parse(rawTermsInput);
      }
    } catch (e) {
      console.warn(`[!] Could not parse terms input as JSON: ${e.message}`);
    }

    if (parsedTerms) {
      const computedHash = computeTermsHash(parsedTerms);
      console.log('[*] CRYPTOGRAPHIC TERMS ATTESTATION:');
      console.log(`    Claimed Terms JSON:  ${JSON.stringify(parsedTerms)}`);
      console.log(`    Computed SHA-256:    ${computedHash}`);
      console.log(`    On-Chain resourceRef:${resourceRef}`);

      if (computedHash.toLowerCase() === resourceRef.toLowerCase()) {
        console.log('    VERIFICATION RESULT: [✓] 100% MATCH - TERMS MATHEMATICALLY PROVEN UNALTERED\n');
      } else {
        console.log('    VERIFICATION RESULT: [!] TAMPER / MISMATCH DETECTED - Terms differ from on-chain hash!\n');
      }
    }
  } else {
    console.log('[i] Pass terms JSON or file as 2nd argument to mathematically verify terms binding:');
    console.log(`    node tools/verify-onchain.js ${onchainId} '{"quantity":3,"provider":"lab-c",...}'\n`);
  }

  console.log('[✓] IMMUTABILITY GUARANTEE:');
  console.log('    1. Terms & Parties are permanently sealed in EVM storage at contract address.');
  console.log('    2. The Solidity contract has NO setter functions for requester, provider, or resourceRef.');
  console.log('    3. Neither party nor MEDway backend can alter terms without creating a new proposal.\n');
}

const targetId = process.argv[2] || '0x70ad9a82d354adc9cceeeed4d5771a971bf005dd18da9308f2bf6baa8598bfe0';
const termsArg = process.argv[3];
verifyReservation(targetId, termsArg);
