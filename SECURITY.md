# Security Policy

At **MEDway**, security, patient privacy, and cryptographic rigor are foundational. Our architecture coordinates clinical resources across regional hospitals while anchoring immutable commitments to the **MST Coral Testnet (Chain ID: 54635)**.

---

## 🛡️ Core Security Architecture & Guarantees

### 1. HIPAA & Zero Protected Health Information (PHI) On-Chain
- Under no circumstances does patient health information (PHI), clinical diagnosis narratives, patient names, or medical record numbers touch the blockchain.
- All on-chain commitments use pseudonymous cryptographic references:
  $$\text{resourceRef} = \text{keccak256}(\text{canonical\_terms})$$
- Mapping between `resourceRef` and actual patient medical charts exists exclusively within institutional Electronic Health Record (EHR) databases behind hospital firewalls.

### 2. Dual-Sided EIP-712 Relayed Meta-Transactions
- Healthcare institutions sign typed off-chain authorization payloads (`ReservationRequest`, `AcceptRequest`, `CapacityLockRequest`).
- An application relayer submits transactions and sponsors all MST gas fees.
- The smart contract recovers the actual signer using `ecrecover` and cross-references the institution against the **On-Chain Institutional Identity Registry**.

### 3. Controlled LLM Reasoning Boundary
- Large Language Models (LLMs) operate strictly in an advisory capacity.
- **Zero Key Access**: LLM modules possess zero private keys, wallet access, or smart contract administrative rights.
- **Schema Validation**: All LLM outputs are validated against strict Pydantic models before consumption by deterministic agents.
- **Anti-Injection Guardrails**: Input sanitizers scrub potential prompt injections and redact PII before invoking LLM APIs.

### 4. Smart Contract Protections
- **Replay Protection**: Per-address sequential nonces (`mapping(address => uint256) public nonces;`).
- **Signature Expiry**: Timestamps enforce strict expiration deadlines (`deadline`).
- **Two-Phase Commit (2PC) Capacity Locking**: Prevents concurrent double-booking during negotiation.
- **Terms Immutability**: Canonical terms hashes cannot be modified once set (zero mutation setters).
- **Consortium Arbitration**: Decentralized multi-party dispute resolution with configurable voting quorums.

---

## 🚨 Reporting a Vulnerability

If you discover a security vulnerability or potential privacy leak within MEDway, please **do not open a public GitHub issue**.

Instead, please send a responsible disclosure report via email:
* **Security Contact**: `security@medway.network` (or contact project maintainers via encrypted message)
* **PGP Key**: Available upon request.

### What to Include:
1. Detailed description of the vulnerability and its potential impact.
2. Step-by-step reproduction instructions or proof-of-concept (PoC) exploit script.
3. Affected components (Smart Contracts, LLM guardrails, Viem bridge, Backend REST API).
4. Proposed mitigation or patch if available.

### Response Timeframe:
* **Initial Acknowledgement**: Within 24 hours.
* **Triage & Assessment**: Within 48 hours.
* **Remediation & Patch**: Promptly coordinated before public disclosure.

