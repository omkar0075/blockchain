# Changelog

All notable changes to the **MEDway** project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.5.0] - 2026-10-31

### Added
- **Adversarial Security & Blockchain Reliability Suites (Tasks 30 & 31)**:
  - 12 comprehensive adversarial threat models in `test_security_adversarial.py` (prompt injection defense, spoofing, replay protection, cross-chain domain checks, PII leakage prevention).
  - 12 blockchain failure and reliability tests in `test_blockchain_reliability.py` (mempool stalling, gas price replacement/speedup, RPC disconnects, reorg state reconciliation).
- **PostgreSQL / Neon Connection Pooling & Advisory Locks**:
  - Upgraded database engine in `backend/app/db.py` to support high-performance connection pooling via `psycopg-pool` and Neon serverless, alongside automatic fallback to SQLite for local development.

---

## [2.4.0] - 2026-10-31

### Added
- **End-to-End AI + Web3 Integration (Task 28)**:
  - Validated complete path from natural language request interpretation through multi-agent negotiation, coordinator sign-off, to on-chain EIP-712 commitment across 10 critical test scenarios.
- **Multi-Hospital Concurrency & Coordination (Task 29)**:
  - Multi-hospital competing resource allocation test suite in `test_multi_hospital_coordination.py` verifying Two-Phase Commit (2PC) capacity locking and fairness in regional surges.

---

## [2.3.0] - 2026-10-31

### Added
- **Agent-LLM Bridge (Task 27)**:
  - Created `AgentLLMBridge` (`backend/app/services/agent_llm_bridge.py`) enabling the 9 deterministic agents to consume AI advisory recommendations while strictly preserving security boundaries.
- **Unified LLM Orchestrator (Task 26)**:
  - Coordinated LLM reasoning pipeline in `backend/app/llm/llm_orchestrator.py` integrating Request Interpretation, Negotiation Advice, and Recovery Analysis into one coherent reasoning layer.

---

## [2.2.0] - 2026-10-31

### Added
- **Controlled LLM Modules (Tasks 22–25)**:
  - `RequestInterpreter` (`backend/app/llm/request_interpreter.py`): Natural language parsing with ambiguity detection and PII scrubbing.
  - `NegotiationAdvisor` (`backend/app/llm/negotiation_advisor.py`): Multi-offer trade-off analysis, scoring, and counter-offer generation.
  - `RecoveryAdvisor` (`backend/app/llm/recovery_advisor.py`): Workflow failure diagnosis and root-cause classification.
  - Provider-agnostic client, schemas, and guardrails in `backend/app/llm/`.

---

## [2.1.0] - 2026-10-31

### Added
- **Persistent Workflow State & Recovery (Task 20)**:
  - Durable workflow state service (`backend/app/services/workflow_service.py`) supporting backend restart recovery, deduplication, and on-chain reconciliation (`reconcile_with_onchain`).
- **Enterprise Agent Observability (Task 21)**:
  - Structured event telemetry, execution profiling, decision logs, and PII redaction (`backend/app/services/agent_observability.py`).

---

## [2.0.0] - 2026-10-30

### Added
- **Autonomous 9-Agent System (Tasks 17–19)**:
  - Formal multi-agent coordination architecture: `RequestAgent`, `DiscoveryAgent`, `ProviderAgent`, `NegotiationAgent`, `VerificationAgent`, `BlockchainExecutionAgent`, `MonitoringAgent`, `RecoveryAgent`, and `ReservationWorkflowCoordinator`.

---

## [1.0.0] - 2026-10-29

### Added
- **Core Smart Contract & Gasless Settlement (Tasks 1–16)**:
  - `contracts/contracts/MEDwayReservation.sol` deployed on MST Coral Testnet (Chain ID `54635`).
  - Dual-sided EIP-712 relayed meta-transactions (100% gasless for healthcare providers).
  - On-Chain Institutional Identity Registry (multi-wallet RBAC).
  - Two-Phase Commit (2PC) Capacity Locking (`lockCapacityWithSig`).
  - Consortium Arbitration Board with multi-arbitrator quorum voting.
  - Decoupled Node.js Viem Bridge (`backend/blockchain-service.js`).
  - Genesis-to-tip SHA-256 tamper-evident hash-chained audit trail.
  - Independent direct on-chain verifier CLI tool (`tools/verify-onchain.js`).
  - React 18 / Vite coordinator dashboard.

