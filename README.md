<img width="2172" height="724" alt="Sleek MEDway Healthcare Header" src="https://github.com/user-attachments/assets/ce537ac5-c544-4b7c-a79d-9e2c1fe80e38" />
<div align="center">

# MEDway

**Autonomous Healthcare Resource Coordination Platform with Controlled LLM Reasoning, Multi-Agent Consensus, and EIP-712 Gasless On-Chain Commitments on MST Coral Testnet**

[![Network: MST Coral](https://img.shields.io/badge/Network-MST_Coral_Testnet_54635-7928CA?style=for-the-badge&logo=ethereum&logoColor=white)](https://coralrpc.mstblockchain.com)
[![Solidity 0.8.34](https://img.shields.io/badge/Solidity-0.8.34_viaIR-363636?style=for-the-badge&logo=solidity&logoColor=white)](contracts/contracts/MEDwayReservation.sol)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI_Python_3.11+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Pytest Suite](https://img.shields.io/badge/Pytest_Backend-293%2F293_Passing-brightgreen?style=for-the-badge&logo=pytest&logoColor=white)](backend/)
[![Hardhat Tests](https://img.shields.io/badge/Hardhat_Contracts-113%2F113_Passing-brightgreen?style=for-the-badge&logo=checkmarx&logoColor=white)](contracts/test)
[![System Verification](https://img.shields.io/badge/System_Verification-8%2F8_Passing-brightgreen?style=for-the-badge&logo=gnubash&logoColor=white)](backend/test-system-verification.py)
[![Viem](https://img.shields.io/badge/Web3_Bridge-Viem_v2.57-black?style=for-the-badge&logo=v&logoColor=white)](https://viem.sh)
[![React 18](https://img.shields.io/badge/Frontend-React_18_Vite-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

[Overview](#-executive-summary--problem-statement) • [Core Pillars](#-core-architectural-pillars) • [System Architecture](#-system-architecture) • [Autonomous 9-Agent Layer](#-autonomous-multi-agent-system) • [Controlled LLM Reasoning](#-controlled-llm-reasoning-layer) • [Agent-LLM Bridge](#-agent-llm-bridge-task-27) • [Live On-Chain Records](#-live-on-chain-verification-summary) • [Smart Contract](#-smart-contract-specification) • [Persistence & Observability](#-state-persistence--agent-observability) • [Test Suite (422+ Tests)](#-comprehensive-testing--verification-suite) • [Getting Started](#-getting-started--local-development) • [API Reference](#-api-specification) • [Security & Compliance](#-security-privacy--compliance)

</div>

---

## 🏥 Executive Summary & Problem Statement

In regional healthcare ecosystems, critical diagnostic, surgical, and therapeutic resources—such as **MRI scanners, CT suites, catheterization labs, specialized ICU beds, and emergency dialysis bays**—suffer from severe, systemic allocation imbalances. While acute emergency centers experience sudden surges with dangerous patient backlogs, neighboring community hospitals, ambulatory surgery clinics, and private diagnostic laboratories frequently operate with idle, unutilized capacity.

```
       [City Hospital A]                          [Central Diagnostic Lab C]
    Surge: 0 Slots Available                      Capacity: 12 Slots Available
    Patient Backlog: CRITICAL                     Resource Utilization: IDLE
             \                                                /
              \--- [ Manual Phone Calls / Fax Chaos ] -------/
                   [ HIPAA / Strict Privacy Barriers ]
                   [ Incompatible Siloed EHR Systems ]
                   [ Double-Booking & Counterparty Risk ]
```

### The Healthcare Coordination Dilemma
1. **The Privacy & Compliance Barrier (HIPAA)**: Healthcare facilities cannot broadcast patient identities, diagnostic summaries, or scheduling calendars over open networks or public ledgers without violating strict statutory regulations.
2. **The Crypto Barrier for Medical Personnel**: Nurses, triage officers, and clinical coordinators cannot be burdened with managing web3 wallets, securing private key seed phrases, purchasing native cryptocurrency, or calculating volatile gas fees during clinical emergencies.
3. **The Unchecked AI Hazard**: While Large Language Models excel at understanding clinical jargon and generating scheduling proposals, **autonomous generative AI must never possess direct authority to sign blockchain transactions or bind institutions to legal or financial liability**.
4. **Counterparty Risk & The Double-Booking Trap**: Without atomic, synchronized state across providers, an idle diagnostic slot broadcast to multiple regional hospitals can be claimed concurrently by two facilities, resulting in life-threatening treatment delays.
5. **Institutional Accreditation & Revocation**: Open decentralized protocols risk impersonation by bad actors or unaccredited clinics unless an authoritative on-chain institutional identity registry continuously enforces accreditation and role-based access control.

---

## 💡 The MEDway Solution

**MEDway** is an enterprise-grade autonomous healthcare resource coordination platform combining **a 9-agent deterministic workflow engine**, a **controlled Large Language Model (LLM) reasoning layer**, and **EIP-712 relayed meta-transactions anchored to the MST Coral Testnet**:

* **Zero Protected Health Information (PHI) On-Chain**: Patient records, medical identifiers, and clinical notes stay strictly inside institutional boundaries. Only pseudonymous cryptographic resource references (`resourceRef = keccak256(...)`) and institution addresses are committed on-chain.
* **100% Gasless for Healthcare Institutions**: Hospitals and diagnostic labs sign typed EIP-712 authorization payloads (`ReservationRequest` and `AcceptRequest`) using standard Ethereum keys with **0 native MST token balance**. A dedicated application relayer submits transactions and sponsors all gas fees.
* **Controlled AI Reasoning with Deterministic Authority**: Natural-language request parsing, negotiation strategies, and failure root-cause analysis are guided by LLMs through strict Pydantic schemas and guardrails. However, **the LLM is strictly advisory**; all execution permissions, boundary validations, and state transitions remain strictly governed by deterministic Python agents and smart contract logic.
* **Autonomous 9-Agent Coordination**: A modular multi-agent system handles resource intake, discovery, inventory verification, counter-offer negotiation, cryptographic validation, relayer dispatch, confirmation tracking, and automated failure recovery.
* **On-Chain Institutional Identity Registry**: The `MEDwayReservation` smart contract enforces multi-wallet institutional accreditation and role-based access control (`Requester`, `Provider`, or `Both`). Unregistered or revoked wallets are strictly blocked at the EVM level.
* **Two-Phase Commit (2PC) On-Chain Capacity Locking**: Providers cryptographically lock provisional capacity (`lockCapacityWithSig`), preventing concurrent double-booking during negotiation before final reservation commitment (`createReservationWithLock`).
* **Cryptographic Terms Immutability & Verifier CLI**: Canonical reservation terms are SHA-256 hashed and anchored on-chain upon creation. The contract contains **zero setter functions** to alter terms. Independent auditors can verify commitments directly against Coral Testnet via raw `eth_call`.
* **Tamper-Evident SHA-256 Hash-Chained Audit Trail**: An append-only audit ledger links each operational event to its predecessor's cryptographic digest. Any manual modification or tampering breaks the mathematical hash chain and is immediately flagged.
* **Dual Persistence Engine**: Enterprise PostgreSQL (Neon serverless with connection pooling and advisory locks) with seamless, zero-config SQLite fallback for local development and offline environments.

---

## 🏛️ Core Architectural Pillars

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   MEDWAY CORE PILLARS                                       │
├──────────────────────────┬─────────────────────────────┬────────────────────────────────────┤
│     ZERO-PII PRIVACY     │     EIP-712 META-TX         │       CONTROLLED AI REASONING      │
│  Patient records stay    │  Hospitals pay 0 gas.       │  LLMs advise via strict Pydantic   │
│  strictly off-chain.     │  Relayer sponsors fees on   │  schemas; 0 blockchain signing     │
│  Only hashes on-chain.   │  Coral Testnet (ID: 54635). │  authority granted to AI models.   │
├──────────────────────────┼─────────────────────────────┼────────────────────────────────────┤
│    9 AUTONOMOUS AGENTS   │   TWO-PHASE CAPACITY LOCK   │     ON-CHAIN IDENTITY REGISTRY     │
│  Deterministic workflow  │  2PC on-chain locking       │  Multi-wallet institutional RBAC   │
│  state machine with full │  eliminates counterparty    │  prevents unauthorized or revoked  │
│  observability & retries.│  double-booking hazards.    │  entities from committing slots.   │
├──────────────────────────┼─────────────────────────────┼────────────────────────────────────┤
│    TERMS IMMUTABILITY    │    TAMPER-EVIDENT AUDIT     │      DUAL-PERSISTENCE REPLICA      │
│  Canonical SHA-256 hash  │  Genesis-to-tip SHA-256     │  Production PostgreSQL (Neon pool) │
│  anchored on-chain with  │  hash chain detects any     │  with seamless local SQLite        │
│  zero mutation setters.  │  backend state tampering.   │  fallback & on-chain reconciliation│
└──────────────────────────┴─────────────────────────────┴────────────────────────────────────┘
```

---

## 📐 System Architecture

MEDway organizes coordination into a clean five-tier architecture with strict unidirectional dependency boundaries:

```mermaid
flowchart TB
    subgraph PRESENTATION["1. Presentation & Client Tier"]
        UI["React 18 Coordinator Dashboard"]
        AUDIT_CLI["Independent Verifier CLI (tools/verify-onchain.js)"]
    end

    subgraph API_GATEWAY["2. Application Gateway & API Tier (FastAPI)"]
        AUTH["RBAC & API Key Anti-Spoofing Guard"]
        REST["REST API Controllers (app/main.py)"]
        BRIDGE_SRV["Agent-LLM Bridge (app/services/agent_llm_bridge.py)"]
    end

    subgraph AI_LAYER["3. Controlled LLM Reasoning Layer (app/llm/)"]
        LLM_ORCH["LLM Orchestrator (llm_orchestrator.py)"]
        INTERP["Request Interpreter (request_interpreter.py)"]
        NEGOT["Negotiation Advisor (negotiation_advisor.py)"]
        RECOV["Recovery Advisor (recovery_advisor.py)"]
        GUARD["Guardrails & PII Scrubbing (guardrails.py)"]
    end

    subgraph AGENT_WORKFLOW["4. Autonomous Multi-Agent & Workflow Tier"]
        COORD["Reservation Workflow Coordinator"]
        subgraph AGENTS["9 Autonomous Deterministic Agents"]
            REQ["RequestAgent"]
            DISC["DiscoveryAgent"]
            PROV["ProviderAgent"]
            NEG["NegotiationAgent"]
            VERIF["VerificationAgent"]
            EXEC["BlockchainExecutionAgent"]
            MON["MonitoringAgent"]
            REC["RecoveryAgent"]
        end
        OBS["Observability Service (agent_observability.py)"]
        WF_SVC["Workflow State Persistence (workflow_service.py)"]
        DB[("PostgreSQL (Neon) / SQLite Hash-Chain DB")]
    end

    subgraph BLOCKCHAIN_TIER["5. Decentralized Settlement Layer (MST Coral Testnet - 54635)"]
        VIEM["Viem Subprocess Bridge (blockchain-service.js)"]
        RELAYER["Sponsored Relayer (0x5E5B...8607)"]
        CONTRACT["MEDwayReservation.sol (0xcd63...bb83)"]
        ID_REG["Institutional Identity Registry"]
        CAP_LOCK["Two-Phase Capacity Locking Engine"]
        DISPUTE["Consortium Arbitration Board (Quorum Voting)"]
    end

    UI -->|"HTTP / JSON"| REST
    REST --> AUTH
    AUTH --> LLM_ORCH
    LLM_ORCH --> GUARD
    GUARD --> INTERP & NEGOT & RECOV
    INTERP & NEGOT & RECOV --> BRIDGE_SRV
    BRIDGE_SRV --> COORD
    COORD --> AGENTS
    AGENTS --> OBS
    AGENTS --> WF_SVC
    WF_SVC --> DB
    EXEC -->|"Subprocess IPC"| VIEM
    VIEM -->|"EIP-712 Meta-Transactions"| RELAYER
    RELAYER -->|"Gas-Sponsored Tx"| CONTRACT
    CONTRACT --- ID_REG & CAP_LOCK & DISPUTE

    %% Independent Direct Trustless Verification
    AUDIT_CLI -.->|"Direct eth_call (Bypass Backend)"| CONTRACT
    UI -.->|"Direct eth_call (Bypass Backend)"| CONTRACT
```

---

## 🤖 Autonomous Multi-Agent System

MEDway coordinates healthcare capacity using **nine specialized, deterministic software agents** operating inside a formal finite state machine (`ReservationWorkflowCoordinator`):

```
                                  [ Incoming Request ]
                                           │
                                           ▼
                                  ┌─────────────────┐
                                  │  RequestAgent   │
                                  └────────┬────────┘
                                           │
                                           ▼
                                  ┌─────────────────┐
                                  │ DiscoveryAgent  │
                                  └────────┬────────┘
                                           │
                                           ▼
                                  ┌─────────────────┐
                                  │  ProviderAgent  │
                                  └────────┬────────┘
                                           │
                                           ▼
                                  ┌─────────────────┐
                             ┌───►│NegotiationAgent ├────┐
                             │    └────────┬────────┘    │ Counter-Offer /
                             │             │             │ Partial Split
                             │             ▼             │
                             │    ┌─────────────────┐    │
                             │    │VerificationAgent│◄───┘
                             │    └────────┬────────┘
                             │             │
                             │             ▼
                             │    ┌─────────────────┐
                             │    │BlockchainAgent  │
                             │    └────────┬────────┘
                             │             │
                             │             ▼
                             │    ┌─────────────────┐
                 Retry /     │    │ MonitoringAgent │
               Alternative   │    └────────┬────────┘
                Provider     │             │ Success / Failure
                             │             ▼
                             │    ┌─────────────────┐
                             └────┤  RecoveryAgent  │
                                  └─────────────────┘
```

### Detailed Agent Roles & Specifications

| # | Agent Name | Primary Responsibility | Input Data | Output Artifact / Action | Safety Invariant |
| :-: | :--- | :--- | :--- | :--- | :--- |
| **1** | **`RequestAgent`** | Ingests, normalizes, and sanitizes healthcare resource requests. Enforces quantity limits, deadlines, and clinical urgency tiers. | Raw request payload or interpreted natural language | Structured `ResourceRequest` domain entity | Strictly rejects un-scrubbed PII or unmapped clinical identifiers. |
| **2** | **`DiscoveryAgent`** | Scans registered regional healthcare institutions; filters by resource type, operational schedule, distance, and capability. | `ResourceRequest`, Provider Registry | Ranked list of prospective candidate providers | Cannot evaluate unaccredited institutions. |
| **3** | **`ProviderAgent`** | Represents individual providers; verifies real-time inventory and availability slots, generating provisional binding offers. | `ResourceRequest`, Provider Inventory | Structured `ProviderOffer` (pricing, slot times, capacity) | Rejects requests exceeding maximum institutional safe operating capacity. |
| **4** | **`NegotiationAgent`** | Manages multi-offer convergence, evaluates trade-offs, and structures counter-offers for partial quantities and split slots. | Multiple `ProviderOffer` objects, Request constraints | Ranked recommendation or structured `CounterOffer` | Never violates maximum requester budget or minimum quantity threshold. |
| **5** | **`VerificationAgent`** | Pre-flight validation gatekeeper. Checks on-chain accreditation, terms matching, expiration windows, and non-tampering. | Proposal, Selected Offer, Canonical Terms | Comprehensive `VerificationResult` & 6-point checklist | If any verification check fails, commitment dispatch is permanently blocked. |
| **6** | **`BlockchainExecutionAgent`**| Coordinates off-chain EIP-712 hash generation, collects requester signature, dispatches to relayer bridge. | Verified Proposal, Authorized Wallet Key | Relayer submission payload, Transaction hash | Never directly holds relayer gas keys; operates through decoupled bridge. |
| **7** | **`MonitoringAgent`** | Polls transaction confirmations, monitors gas mempool replacement (speedup), detects chain reorgs, and verifies block depth. | Transaction hash, Relayer receipt, Chain ID | Confirmation receipt, block number, finalized state | Requires configured block confirmation depth before declaring success. |
| **8** | **`RecoveryAgent`** | Autonomous incident response. Classifies execution failures (timeouts, reverts, drops) and triggers retries, renegotiations, or escalations. | Workflow error context, failure payload | `RecoveryPlan` (Action: `RETRY`, `ALT_PROVIDER`, `ESCALATE`) | Cannot trigger privileged recovery without audit logging and state validation. |
| **9** | **`ReservationWorkflowCoordinator`** | Finite state machine orchestrator managing lifecycle transitions, event dispatching, persistence snapshots, and timeouts. | Agent outputs, coordinator triggers | End-to-end coordinated reservation state | Strictly prevents illegal state transitions and duplicate workflow execution. |

---

## 🧠 Controlled LLM Reasoning Layer

To handle the complexity and nuance of clinical workflows without introducing stochastic hazards, MEDway implements a **Controlled AI Architecture**:

> [!IMPORTANT]
> **Fundamental Safety Rule**: The LLM acts strictly as an **Advisory Intelligence Layer**. It has **zero cryptographic key access**, cannot sign transactions, cannot call smart contracts, and cannot alter deterministic agent state machines directly. Every LLM output is strictly parsed and validated against typed Pydantic models before consumption.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       CONTROLLED LLM REASONING LAYER                        │
├─────────────────────────┬─────────────────────────┬─────────────────────────┤
│   REQUEST INTERPRETER   │   NEGOTIATION ADVISOR   │    RECOVERY ADVISOR     │
│  Extracts type, qty,    │  Evaluates multi-offer  │  Analyzes failures,     │
│  urgency, deadline from │  trade-offs, scores     │  diagnoses root cause,  │
│  natural language text. │  providers, suggests    │  and recommends repair  │
│  Detects missing info.  │  counter-offers.        │  actions or escalation. │
├─────────────────────────┴─────────────────────────┴─────────────────────────┤
│                             SAFETY & GUARDRAILS                             │
│   • Input/Output Guardrails (app/llm/guardrails.py): PII scrubbing, prompt  │
│     injection defense, boundary validation, and schema enforcement.         │
│   • Deterministic Fallback: Every LLM module implements a deterministic     │
│     rule-based fallback that activates instantly if the API drops.          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1. Request Interpreter (`backend/app/llm/request_interpreter.py`)
Converts unstructured medical requests into structured, validated `ResourceRequest` models:
* **Natural-Language Understanding**: Translates colloquial clinical queries (e.g., *"We have two acute stroke patients needing emergency head CT with contrast in North Ward within 90 minutes"*) into standard domain schemas.
* **Ambiguity Detection**: Automatically detects missing quantities or undefined timeframes and generates clarifying questions rather than hallucinating details.
* **PII Redaction**: Strips patient names, medical record numbers, and protected health information before domain processing.
* **Deterministic Fallback**: Uses regex and keyword heuristics if LLM APIs are unreachable.

### 2. Negotiation Advisor (`backend/app/llm/negotiation_advisor.py`)
Assists hospital coordinators in evaluating complex, competing multi-provider bids:
* **Multi-Attribute Trade-off Analysis**: Weighs cost, transit distance, provider rating, urgency, and equipment specifications.
* **Partial Allocation Structuring**: Suggests counter-offers when a provider can only fulfill partial quantities (e.g., 2 of 3 requested slots).
* **Transparent Justification**: Generates human-readable explanations detailing *why* a particular facility is recommended over alternatives.
* **Deterministic Fallback**: Fallback ranker uses weighted Euclidean distance-cost scoring.

### 3. Recovery Advisor (`backend/app/llm/recovery_advisor.py`)
Autonomous failure analysis and incident recovery guidance:
* **Root-Cause Classification**: Accurately categorizes failures into `PROVIDER_UNAVAILABLE`, `INSUFFICIENT_CAPACITY`, `TRANSACTION_REVERTED`, `TIMEOUT`, `VERIFICATION_FAILED`, or `ARBITRATION_REQUIRED`.
* **Action Recommendation**: Recommends appropriate mitigation strategies (`RETRY`, `ALTERNATIVE_PROVIDER`, `RENEGOTIATE_TERMS`, or `HUMAN_ESCALATION`).
* **Deterministic Fallback**: Rule-based error matrix guarantees predictable recovery recommendations even during network partitions.

### 4. LLM Orchestrator (`backend/app/llm/llm_orchestrator.py`)
Unified coordinator binding all three reasoning components into an integrated pipeline that mirrors the deterministic workflow stages while strictly preserving correlation IDs and audit events.

---

## 🌉 Agent-LLM Bridge (Task 27)

The `AgentLLMBridge` (`backend/app/services/agent_llm_bridge.py`) forms the controlled boundary between the 9 deterministic workflow agents and the LLM reasoning layer:

```
[ Deterministic Agents ] ◄─── (Minimum Necessary Data) ───► [ Agent-LLM Bridge ] ◄───► [ LLM Layer ]
   • RequestAgent               • Preserves Workflow ID          • Validates Pydantic Output
   • DiscoveryAgent             • Redacts PII & Secrets          • Applies Guardrails
   • NegotiationAgent           • Non-Blocking Fallbacks         • Returns AIRecommendation
   • RecoveryAgent              • Records Observability Logs     • 0 Execution Privileges
```

### Integration Capabilities Matrix

| Deterministic Agent | Integrated LLM Capability | Bridge Method | Fallback Strategy |
| :--- | :--- | :--- | :--- |
| **`RequestAgent`** | Request Interpretation | `interpret_request()` | Regex & domain keyword parser |
| **`DiscoveryAgent`** | Provider Suitability Scoring | `explain_provider_selection()` | Deterministic rank score breakdown |
| **`ProviderAgent`** | Offer Condition Explanation | `explain_offer_terms()` | Standard terms summary formatter |
| **`NegotiationAgent`** | Negotiation Strategy & Counter-Offers | `advise_negotiation()` | Weighted cost/distance utility ranker |
| **`VerificationAgent`** | Verification Failure Analysis | `explain_verification_failure()` | Rule-based checklist failure printer |
| **`RecoveryAgent`** | Recovery Strategy Recommendation | `advise_recovery()` | Deterministic error code lookup table |
| **`MonitoringAgent`** | Blockchain Event Summarization | `summarize_transaction()` | Standard receipt parameter formatter |
| **`BlockchainAgent`** | **NO LLM AUTHORITY** | *None (Security Guard)* | 100% Deterministic EIP-712 validation |

---

## ⚡ Live On-Chain Verification Summary

The complete end-to-end lifecycle is live and verified on the **MST Coral Testnet (Chain ID: 54635)**.

### Gasless EIP-712 Lifecycle Sequence

```mermaid
sequenceDiagram
    autonumber
    actor HospA as Hospital A (Requester)
    participant Coord as Workflow Coordinator
    participant Bridge as Viem Bridge (Node.js)
    participant Relayer as Relayer (0x5E5B...)
    participant Coral as MEDwayReservation.sol
    actor LabC as Lab C (Provider)

    Note over HospA,Coord: Phase 1: AI Request Interpretation & Discovery
    HospA->>Coord: Natural Language: "Need 3 MRI slots urgently"
    Coord->>Coord: LLM Request Interpreter structures request -> DiscoveryAgent finds Lab C
    Coord-->>HospA: Proposed Allocation (Lab C: 3 slots @ 150 MST)

    Note over HospA,Coral: Phase 2: Gasless On-Chain Creation (EIP-712)
    HospA->>Coord: Institutional Sign-Off (Human Gate)
    Coord->>Bridge: create_onchain_reservation(hospital-a, lab-c)
    Note over Bridge: Hospital A signs ReservationRequest off-chain (0 MST Spent)
    Bridge->>Relayer: Signed EIP-712 Payload
    Relayer->>Coral: createReservationWithSig() [Relayer Pays Gas]
    Coral-->>Coral: Check Requester Accreditation, Verify Sig, Store termsHash
    Coral-->>Bridge: Event: ReservationCreated (Tx: 0xeb89...)
    Bridge-->>Coord: Confirmed in Block #405589+

    Note over LabC,Coral: Phase 3: Gasless Provider Acceptance (EIP-712)
    LabC->>Coord: Accept Commitment
    Coord->>Bridge: accept_onchain_reservation(reservationId, lab-c)
    Note over Bridge: Lab C signs AcceptRequest off-chain (0 MST Spent)
    Bridge->>Relayer: Signed EIP-712 Payload
    Relayer->>Coral: acceptReservationWithSig() [Relayer Pays Gas]
    Coral-->>Coral: Verify Provider Sig, Check Accreditation, Transition to Committed
    Coral-->>Bridge: Event: ReservationCommitted
    Bridge-->>Coord: Commitment Finalized & Immutable

    Note over HospA,Coral: Phase 4: Independent Direct Attestation
    HospA->>Coral: eth_call (reservations, direct to RPC)
    Coral-->>HospA: Returns on-chain resourceRef & termsHash
    Note over HospA: Computes SHA-256(canonical_terms) -> 100% Mathematical Match!
```

### Live On-Chain Parameters & Addresses

| Parameter | Live On-Chain Value / Target |
| :--- | :--- |
| **Smart Contract Address** | [`0xcd630b8e555e0751afbb9ae511869110d361bb83`](https://coralrpc.mstblockchain.com) |
| **Network Name & Chain ID**| **MST Coral Testnet** (`54635`) |
| **JSON-RPC Endpoint** | `https://coralrpc.mstblockchain.com` |
| **Contract Owner / Arbiter** | `0x5E5BB85E3f92389a75e8b555490c99af8B0e8607` |
| **Relayer Wallet (Gas Sponsor)**| `0x5E5BB85E3f92389a75e8b555490c99af8B0e8607` (~149.76 MST) |
| **Contract Deployment Block** | Block **#405589** (Tx: `0x2a3c8317b9361253083427d5bfe81106093ba9b4a7f323a478629a8e8926ebc8`) |
| **Hospital A Identity** | `0x4dB8336edB0af7FeD1311312081689993F80a18B` (Role: **Requester** · Gas Spent: **0 MST**) |
| **Hospital B Identity** | `0x70997970c51812e33987251372ecdbc2b3f3bb6e` (Role: **Requester** · Gas Spent: **0 MST**) |
| **Lab C Identity** | `0xb05049cc2A5a1c28979DEafc82B82749681DA92C` (Role: **Provider** · Gas Spent: **0 MST**) |
| **Capacity Protocol** | **Two-Phase Commit (2PC)**: `lockCapacityWithSig` $\rightarrow$ `createReservationWithLock` |
| **Dispute Arbitration Window** | **604,800 seconds (7 days)** |

---

## 📜 Smart Contract Specification

The core contract [`contracts/contracts/MEDwayReservation.sol`](contracts/contracts/MEDwayReservation.sol) is compiled with Solidity `0.8.34` using `viaIR: true` and 200 optimizer runs.

### State Machine Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Proposed: createReservationWithSig (Active Requester)
    [*] --> Locked: lockCapacityWithSig (Provider 2PC Lock)
    Locked --> Proposed: createReservationWithLock
    
    Proposed --> Committed: acceptReservationWithSig (Active Provider)
    Proposed --> Cancelled: cancelReservation (Requester or Provider)
    Proposed --> Expired: expireReservation (Past expiresAt)
    
    Committed --> Fulfilled: fulfillReservation (Provider Attestation)
    Committed --> Cancelled: cancelReservation (Mutual / Emergency)
    Committed --> Disputed: disputeReservation (Within 7-day window)
    
    Fulfilled --> Disputed: disputeReservation (Within 7-day window)
    Fulfilled --> [*]: Settled
    
    Disputed --> Fulfilled: resolveDispute (Consortium Quorum Ruling)
    Disputed --> Cancelled: resolveDispute (Consortium Quorum Ruling)
    
    Cancelled --> [*]
    Expired --> [*]
```

### Smart Contract Functions Directory

| Function | Access Control | Purpose | Estimated Gas |
| :--- | :--- | :--- | :--- |
| `registerInstitution(id, name, role)` | Contract Owner | Accredit new hospital/lab with role (`Requester`, `Provider`, `Both`) | ~98,000 |
| `authorizeWallet(id, wallet)` | Contract Owner | Authorize secondary wallet for an accredited institution | ~45,000 |
| `revokeWallet(wallet)` | Contract Owner | Revoke individual institutional wallet access | ~28,000 |
| `lockCapacityWithSig(...)` | Public (Relayer) | **2PC Lock**: Provider signs off-chain capacity reservation hold | ~72,000 |
| `createReservationWithLock(...)` | Public (Relayer) | Creates proposal linked to pre-locked provider capacity | ~195,000 |
| `createReservationWithSig(...)` | Public (Relayer) | Creates proposal with EIP-712 requester signature | ~185,000 |
| `acceptReservationWithSig(...)` | Public (Relayer) | Provider commits with EIP-712 signature | ~42,000 |
| `fulfillReservation(id)` | Authorized Provider| Attests resource delivery; starts 7-day dispute window | ~38,000 |
| `cancelReservation(id)` | Requester / Provider| Cancels unfulfilled reservation | ~34,000 |
| `expireReservation(id)` | Anyone (Keeper) | Sweeps unaccepted reservations past `expiresAt` | ~31,000 |
| `disputeReservation(id, evidenceHash)`| Requester / Provider| Opens dispute; anchors cryptographic evidence digest | ~48,000 |
| `addArbitrator(wallet)` | Contract Owner | Appoints consortium arbitration board member | ~46,000 |
| `setArbitrationQuorum(quorum)` | Contract Owner | Sets multi-party dispute voting quorum threshold | ~28,000 |
| `castArbitrationVote(id, ruling, ...)`| Board Arbitrator | Casts vote; auto-resolves when quorum is reached | ~52,000 |
| `rotateReservationWallet(id, newWallet)`| Institution Owner | Rotates key on active commitment without disruption | ~36,000 |
| `getReservation(id)` | Public View | View on-chain reservation status, terms hash, participants | Free (0 gas) |
| `isRequesterWallet(wallet)` | Public View | Verifies if address is accredited active requester | Free (0 gas) |
| `isProviderWallet(wallet)` | Public View | Verifies if address is accredited active provider | Free (0 gas) |

---

## 💾 State Persistence & Agent Observability

### 1. Persistent Workflow State (`backend/app/services/workflow_service.py`)
To prevent workflow loss across server restarts, network reconnects, or process terminations (Task 20), MEDway provides durable state persistence:
* **Append-Only Transition Log**: Every state change records `workflow_id`, `from_state`, `to_state`, `trigger_event`, timestamps, and contextual metadata.
* **Resumption & Deduplication Engine**: Automatically identifies interrupted workflows upon backend restart and safely resumes execution without duplicate blockchain submissions.
* **On-Chain Reconciliation (`reconcile_with_onchain`)**: Queries live smart contract storage via JSON-RPC to reconcile off-chain database records against authoritative EVM state.
* **Dual Persistence Layer (`backend/app/db.py`)**:
  - **Production**: PostgreSQL / Neon connection pool with transaction isolation and advisory locks.
  - **Local/Testing**: Zero-dependency SQLite engine with SHA-256 hash-chaining.

### 2. Enterprise Agent Observability (`backend/app/services/agent_observability.py`)
Provides complete visibility into agent decisions and workflow execution (Task 21):
* **Structured Event Tracing**: Tracks `AGENT_START`, `AGENT_COMPLETE`, `DECISION`, `CHECKPOINT`, `ERROR`, and `RETRY` events with nanosecond timestamps.
* **Correlation IDs**: Correlates actions across `workflow_id`, `agent_name`, `parent_event_id`, and `execution_step`.
* **Decision Rationales**: Logs explicit criteria for provider selection, counter-offer generation, and recovery actions.
* **Secret & PII Sanitization**: Automatically scrubs API keys, private keys, authorization tokens, and patient identifiers before persisting.
* **Non-Blocking Fault Isolation**: Observability storage errors never interrupt active clinical workflows.

---

## 🧪 Comprehensive Testing & Verification Suite

MEDway is backed by an industry-grade automated testing suite of **over 422 tests** spanning all 31 tasks:

| Test Suite | Framework | Count | Scope |
| :--- | :--- | :---: | :--- |
| **Contract Hardhat Suite** | Hardhat / Viem / Foundry | **113** | Solidity invariants, EIP-712 verification, RBAC, 2PC capacity locking, arbitration |
| **Backend Pytest Suite** | Pytest / AnyIO | **293** | 9 Agents, Persistence, Observability, LLM Modules, Bridge, E2E AI+Web3, Concurrency, Security, Reliability |
| **System Verification Suite** | Python / Requests / Web3 | **8** | Database recovery, hash-chain tamper detection, live Coral RPC attestation, evidence integrity |
| **REST API Test Suite** | Python / HTTPX | **8** | REST endpoints, authentication headers, anti-spoofing guards, health checks |
| **TOTAL VERIFIED TESTS** | | **422** | **100% Pass Rate across all modules** |

---

### Pytest Backend Breakdown (293 Tests)

```text
backend/
├── test_agent_foundation.py              (Core agent entities, domain validation)
├── test_agent_workflow.py                (Deterministic 9-agent workflow execution)
├── test_service_workflow.py              (Task 20: Persistent state, restarts, reconciliation)
├── test_service_observability.py         (Task 21: Agent telemetry, decision logs, PII scrubbing)
├── test_llm_foundation.py                (Task 22: Controlled LLM client, schemas, guardrails)
├── test_llm_request_interpreter.py       (Task 23: Natural-language request interpretation)
├── test_llm_negotiation_advisor.py       (Task 24: Offer evaluation, counter-offers, trade-offs)
├── test_llm_recovery_advisor.py          (Task 25: Failure classification, root-cause analysis)
├── test_llm_orchestrator.py              (Task 26: Coordinated LLM reasoning layer)
├── test_agent_llm_bridge.py              (Task 27: Agent-LLM Bridge integration & policy boundary)
├── test_ai_web3_integration.py           (Task 28: End-to-end AI + Web3 lifecycle integration)
├── test_multi_hospital_coordination.py   (Task 29: Concurrency, competing requests, 2PC lock sharing)
├── test_security_adversarial.py          (Task 30: 12 Threat models, prompt injection, replay guards)
└── test_blockchain_reliability.py        (Task 31: 12 Failure modes, mempool replacement, reorgs)
```

#### 1. End-to-End AI + Web3 Integration (`test_ai_web3_integration.py` - Task 28)
* Tests natural language request intake flowing through AI interpretation, multi-agent matching, institutional sign-off, and on-chain EIP-712 commitment.
* Verifies AI negotiation advice in multi-offer bidding scenarios.
* Validates automated recovery recommendations during provider capacity drops.
* Confirms zero-data-loss fallback resilience when LLM APIs are offline.

#### 2. Multi-Hospital Coordination & Concurrency (`test_multi_hospital_coordination.py` - Task 29)
* Simulates multiple hospitals competing simultaneously for scarce regional ICU/CT capacity.
* Verifies Two-Phase Commit (2PC) capacity locking, preventing over-allocation and race conditions.
* Evaluates emergency prioritization and fair queuing algorithms under severe surge loads.

#### 3. Security & Adversarial Attack Suite (`test_security_adversarial.py` - Task 30)
* **Prompt Injection Defense**: Evaluates LLM resilience against adversarial prompts attempting to bypass authorization or extract system prompts.
* **Wallet Spoofing & RBAC Bypasses**: Confirms rejection of requests using mismatched or unaccredited institutional credentials.
* **EIP-712 Replay Attacks**: Verifies rejection of replayed signatures across different chains or invalidated nonces.
* **PII Exfiltration Shields**: Proves patient data cannot leak into logs or blockchain parameters.

#### 4. Blockchain Reliability & Failure Modes (`test_blockchain_reliability.py` - Task 31)
* **Mempool Stalling & Replacement**: Tests transaction speedups via replacement gas pricing.
* **RPC Dropouts & Failover**: Verifies graceful retry handling during node disconnects.
* **Chain Reorganization & Drift Reconciliation**: Validates state recovery and divergence detection when local state lags behind on-chain events.

---

### Executing the Test Suites

#### 1. Run Smart Contract Tests (113 Tests)
```bash
cd contracts
npx hardhat test
```

#### 2. Run Backend Pytest Suite (293 Tests)
```bash
cd backend
pytest -v
```

#### 3. Run System Verification Suite (8 End-to-End Modules)
```bash
python backend/test-system-verification.py
```

#### 4. Run REST API Endpoint Tests
```bash
python backend/test_api_endpoints.py
```

---

## 🔍 Independent On-Chain Terms Verifier Tool

Participating healthcare institutions and regulatory auditors can independently verify commitments directly from Coral Testnet without running or trusting the MEDway backend using [`tools/verify-onchain.js`](tools/verify-onchain.js).

### Usage:
```bash
node tools/verify-onchain.js <reservationId> [termsJsonOrFilePath]
```

### Execution Example:
```bash
node tools/verify-onchain.js 0xb550f09d... '{"proposal_id":"prop-01","provider":"lab-c","quantity":3,"requester":"hospital-a","resource_type":"diagnostic_slot"}'
```

```text
========================================================================
 MEDWAY INDEPENDENT ON-CHAIN VERIFIER & TERMS ATTESTATION TOOL
========================================================================
[*] Direct Blockchain RPC:  https://coralrpc.mstblockchain.com
[*] Target Smart Contract:  0xcd630b8e555e0751afbb9ae511869110d361bb83
[*] Target Reservation ID:  0xb550f09d...

[+] DIRECT ON-CHAIN CONFIRMATION RECEIVED IN 118ms
------------------------------------------------------------------------
  Reservation ID:      0xb550f09d...
  Current Status:      COMMITTED (Code: 1)
  Requester Address:   0x4dB8336edB0af7FeD1311312081689993F80a18B
  Provider Address:    0xb05049cc2A5a1c28979DEafc82B82749681DA92C
  On-Chain Terms Hash: 0xe0e807584b6714babc32330a84dfe306517bb3d276580bbfe55e09e69070e360
  Expires At:          2026-10-31T00:47:38.000Z
------------------------------------------------------------------------
[*] CRYPTOGRAPHIC TERMS ATTESTATION:
    Claimed Terms JSON:  {"proposal_id":"prop-01","provider":"lab-c",...}
    Recomputed SHA-256:  0xe0e807584b6714babc32330a84dfe306517bb3d276580bbfe55e09e69070e360
    On-Chain Stored Hash:0xe0e807584b6714babc32330a84dfe306517bb3d276580bbfe55e09e69070e360
    Attestation Result:  ✓ 100% MATHEMATICAL MATCH
[✓] Terms verified! Agreed quantities, provider, and requester cannot be modified.
```

---

## 📂 Repository Structure & Clean Architectural Boundaries

```text
blockchain/
├── 📁 backend/                             # Core Application, AI Layer & Web3 Bridge
│   ├── 📁 app/                             # Python domain & application services
│   │   ├── 📁 llm/                         # Controlled LLM Reasoning Layer (Tasks 22-26)
│   │   │   ├── client.py                   # Async client with retry & timeout handling
│   │   │   ├── guardrails.py               # Input/output schemas, PII scrubbing, injection shields
│   │   │   ├── llm_orchestrator.py         # Unified LLM reasoning coordinator (Task 26)
│   │   │   ├── negotiation_advisor.py      # Multi-offer evaluator & counter-offer advisor (Task 24)
│   │   │   ├── prompts.py                  # Versioned, audited system prompts
│   │   │   ├── provider.py                 # Provider-agnostic LLM interface (OpenAI/Anthropic/Gemini/Mock)
│   │   │   ├── recovery_advisor.py         # Failure diagnosis & recovery advisor (Task 25)
│   │   │   ├── request_interpreter.py      # Natural-language request parser (Task 23)
│   │   │   └── schemas.py                  # Strict Pydantic models for all LLM inputs/outputs
│   │   ├── 📁 services/                    # Infrastructure & Support Services
│   │   │   ├── agent_llm_bridge.py         # Deterministic Agent <-> LLM Bridge (Task 27)
│   │   │   ├── agent_observability.py      # Event tracing, metrics, decision logs (Task 21)
│   │   │   └── workflow_service.py         # Persistent state, recovery & on-chain reconciliation (Task 20)
│   │   ├── 📁 workflows/                   # Workflow Orchestration
│   │   │   └── reservation_workflow.py     # 9-Agent deterministic finite state machine (Tasks 17-19)
│   │   ├── agents.py                       # Core deterministic agent definitions
│   │   ├── auth.py                         # RBAC, API credential guard & anti-spoofing
│   │   ├── blockchain.py                   # IPC adapter calling Node.js Viem bridge
│   │   ├── data.py                         # Seed data: initial hospital & provider capacities
│   │   ├── db.py                           # PostgreSQL (Neon pool) + SQLite fallback with hash-chaining
│   │   ├── main.py                         # FastAPI REST controller & middleware
│   │   └── models.py                       # Clean domain entities (Request, Offer, Commitment)
│   ├── blockchain-service.js               # Decoupled Node.js Viem Bridge (CJS, no Hardhat in prod)
│   ├── check-coral.js                      # Diagnostic probe: tests RPC latency, block height & node health
│   ├── verify-contract-state.js            # Diagnostic probe: inspects on-chain reservation mappings
│   ├── test-system-verification.py         # 8-module end-to-end system verification harness
│   ├── test_*.py                           # Comprehensive Pytest test suites (293 tests)
│   ├── requirements.txt                    # Python dependencies (FastAPI, Uvicorn, Pydantic, HTTPX, Pytest)
│   ├── package.json                        # Node.js dependencies for Viem bridge (viem, dotenv)
│   └── .env                                # Local secrets & private keys (git-ignored)
│
├── 📁 contracts/                           # Consensus & Settlement Layer (Solidity / Hardhat 3)
│   ├── 📁 contracts/                       # Smart contracts
│   │   ├── MEDwayReservation.sol          # Core contract: EIP-712 meta-tx, Identity Registry, 2PC Lock
│   │   ├── Counter.sol                     # Reference contract
│   │   └── Counter.t.sol                   # Foundry unit & fuzz tests
│   ├── 📁 scripts/                         # Operational deployment & test runners
│   │   ├── deploy-upgraded-coral.cjs       # Production deployment & initial institution registration
│   │   ├── relay-coral-reservation.ts      # Live sponsored gasless reservation execution
│   │   └── test-gasless-lifecycle.ts       # End-to-end gasless simulation lifecycle
│   ├── 📁 test/                            # Hardhat test specifications
│   │   └── MEDwayReservation.ts           # 113-test comprehensive Hardhat suite
│   ├── hardhat.config.ts                   # Hardhat config (Solidity 0.8.34, viaIR, MST Coral target)
│   └── package.json                        # Development dependencies (Hardhat, Viem, Toolbox)
│
├── 📁 frontend/                            # Presentation Layer (React 18 + Vite)
│   ├── 📁 src/                             # UI application code
│   │   ├── App.tsx                         # Coordinator dashboard with Direct RPC Verify modal
│   │   ├── main.tsx                        # React DOM root entry point
│   │   └── styles.css                      # Modern healthcare design system
│   ├── vite.config.ts                      # Vite configuration & development proxy
│   └── package.json                        # Frontend dependencies (React 18, Vite, Lucide Icons)
│
├── 📁 tools/                               # Independent Trustless Attestation Tools
│   └── verify-onchain.js                   # Zero-backend auditor CLI reading raw EVM storage
│
├── .env.example                            # Comprehensive environment variables template
├── .gitignore                              # Git exclusion rules
├── LICENSE                                 # MIT Open Source License
└── README.md                               # Complete project documentation & runbooks
```

---

## 🚀 Getting Started & Local Development

### Prerequisites
* **Node.js**: v20.x or v22.x LTS
* **Python**: v3.11 or higher
* **Git**: Installed and configured

---

### Step 1: Clone Repository & Configure Environment
```bash
git clone https://github.com/omkar0075/blockchain.git
cd blockchain

# Copy template to backend/.env and contracts/.env
cp .env.example backend/.env
cp .env.example contracts/.env
```

Your `backend/.env` should contain:
```env
# MST Coral Testnet
CORAL_RPC_URL=https://coralrpc.mstblockchain.com
CHAIN_ID=54635
CONTRACT_ADDRESS=0xcd630b8e555e0751afbb9ae511869110d361bb83
MEDWAY_CONTRACT_ADDRESS=0xcd630b8e555e0751afbb9ae511869110d361bb83

# Relayer Gas Key (Sponsors Gas on Coral)
CORAL_PRIVATE_KEY=0x5E5BB85E3f92389a75e8b555490c99af8B0e8607...

# Persistent Institution Keys (Off-Chain Signers - 0 MST Needed)
HOSPITAL_A_PRIVATE_KEY=0xdb005ea813f1e7bc63ba1c81cc33b9820502b6a40abfa065b8330fc08f5a607b
HOSPITAL_A_ADDRESS=0x4dB8336edB0af7FeD1311312081689993F80a18B
LAB_C_PRIVATE_KEY=0x857d17464bde4ab400af5f87be5a92388514649da5d39bc6df9d68c85ae71b8c
LAB_C_ADDRESS=0xb05049cc2A5a1c28979DEafc82B82749681DA92C

# Database (Optional PostgreSQL/Neon connection; defaults to SQLite if omitted)
# DATABASE_URL=postgresql://user:password@ep-xyz.neon.tech/medway?sslmode=require

# LLM Configuration (Optional API keys; uses deterministic mock provider if omitted)
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...
# GEMINI_API_KEY=AIza...
```

---

### Step 2: Install Dependencies & Verify Blockchain Bridge
```bash
# 1. Install contract dependencies
cd contracts
npm install

# 2. Install backend dependencies
cd ../backend
npm install
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt

# 3. Test standalone Viem blockchain service
node blockchain-service.js status
```

---

### Step 3: Run the FastAPI Backend Server
```bash
cd backend
uvicorn app.main:app --reload --port 8000
```
Interactive Swagger API documentation: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

---

### Step 4: Run the React Frontend Dashboard
```bash
cd frontend
npm install
npm run dev
```
Open [http://localhost:5173](http://localhost:5173) in your browser.

---

## 📡 API Specification

| Method | Endpoint | Description | Sample Response / Status |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Health check & Coral Testnet connection status | `{ "status": "ok", "blockchain": { "connected": true, "latest_block": 405589 } }` |
| `GET` | `/institutions` | List participating accredited healthcare centers | `[ { "id": "hospital-a", "name": "City Hospital A", "role": "requester" }, ... ]` |
| `GET` | `/institutions/{id}/onchain` | Verify accreditation directly from Coral contract | `{ "active": true, "role": "Requester", "isRequesterWallet": true }` |
| `POST` | `/requests` | Ingest resource shortage; activates multi-agent discovery | `{ "proposal_id": "...", "selected_provider": "lab-c", "status": "proposed" }` |
| `POST` | `/proposals/{id}/authorize`| Coordinator human sign-off; triggers on-chain EIP-712 creation | `{ "status": "authorized", "commitment": { "tx_hash": "0xeb...", "block": 405591 } }` |
| `POST` | `/commitments/{id}/accept` | Provider EIP-712 acceptance on Coral Testnet | `{ "status": "committed", "blockchain": { "txHash": "0x207..." } }` |
| `GET` | `/commitments/{id}/onchain` | Inspect live contract storage slot for commitment | `{ "onchain": true, "contract_state": { "status": "Committed", "statusCode": 1 } }` |
| `POST` | `/commitments/{id}/fulfill`| Attest resource delivery; initiates 7-day dispute window | `{ "commitment": { "status": "fulfilled" } }` |
| `POST` | `/commitments/{id}/dispute`| Open dispute with on-chain SHA-256 evidence anchoring | `{ "commitment": { "status": "disputed" } }` |
| `POST` | `/commitments/{id}/resolve`| Consortium arbitrator ruling on Coral Testnet | `{ "commitment": { "status": "cancelled" } }` |
| `GET` | `/audit` | Retrieve complete hash-chained audit ledger | `[ { "id": 1, "action": "CREATE_REQUEST", "event_hash": "0x..." }, ... ]` |
| `GET` | `/audit/verify` | Verify genesis-to-tip audit trail cryptographic integrity | `{ "valid": true, "total_records": 35, "root_event_hash": "0x..." }` |
| `POST` | `/evidence` | Upload and SHA-256 anchor off-chain clinical evidence | `{ "success": true, "computed_sha256": "0x...", "verified": true }` |
| `GET` | `/evidence/{id}` | Retrieve and verify clinical evidence document digest | `[ { "evidence_type": "clinical_log", "verified": true }, ... ]` |

---

## 🔒 Security, Privacy & Compliance

### 1. Zero-PII Policy & HIPAA Alignment
* **Strict Off-Chain Boundary**: Patient names, medical record numbers, dates of birth, and clinical diagnosis narratives never enter smart contract parameters or public logs.
* **Pseudonymous Cryptographic Hashes**: Resources are tracked via `resourceRef = keccak256(canonical_terms)`, ensuring that unauthorized third parties cannot infer medical or personal details from blockchain data.

### 2. On-Chain Role-Based Access Control (RBAC)
* Only registered requester wallets can initiate reservations.
* Only registered provider wallets can accept reservations or lock capacity.
* Unregistered or revoked wallets fail transaction execution with custom Solidity errors (`UnauthorizedRequester`, `UnauthorizedProvider`).

### 3. Cryptographic Replay Protection
* The smart contract maintains per-institution replay counters: `mapping(address => uint256) public nonces;`.
* Every signed authorization (`ReservationRequest`, `AcceptRequest`, `CapacityLockRequest`) encodes the signer's current nonce.
* Replaying an identical signature reverts with `InvalidSignature`.

### 4. Controlled AI Safety & Anti-Hallucination Policy
* The LLM operates purely in an advisory capacity.
* Strict Pydantic validators reject unexpected keys or malformed responses.
* If the LLM generates inconsistent values or unavailable providers, deterministic fallbacks immediately take over.

### 5. Consortium Arbitration Board & Dispute Windows
* Following fulfillment attestation, a `DISPUTE_WINDOW` of **7 days** (604,800 seconds) is initiated.
* If clinical quality or service delivery fails, either party can raise an on-chain dispute by submitting an immutable `evidenceHash`.
* Multi-arbitrator quorum voting enables fair, decentralized dispute resolution without single-operator centralization.

---

## 🗺️ Roadmap & Completed Milestones

- [x] **Tasks 1–16**: Solidity `MEDwayReservation.sol` smart contract with EIP-712 meta-transactions and MST Coral Testnet deployment
- [x] **Tasks 1–16**: Pure Viem standalone Node.js bridge service (decoupled from Hardhat)
- [x] **Tasks 1–16**: Dual-sided gasless lifecycle execution (Hospital A proposal + Lab C acceptance)
- [x] **Tasks 1–16**: On-Chain Institutional Identity Registry with multi-wallet RBAC
- [x] **Tasks 1–16**: Cryptographic Terms Immutability & Independent Zero-Backend Verifier Tool
- [x] **Tasks 1–16**: Tamper-Evident SHA-256 Hash-Chained Audit Trail & Off-Chain Evidence Vault
- [x] **Tasks 1–16**: Consortium Arbitration Board with multi-arbitrator quorum voting & auto-finalization
- [x] **Tasks 17–19**: Autonomous 9-Agent deterministic coordination layer & finite state machine
- [x] **Task 20**: Durable workflow state persistence, restart resumption, and on-chain state reconciliation
- [x] **Task 21**: Enterprise agent observability service with decision logs, execution profiling, and PII redaction
- [x] **Task 22**: Controlled LLM integration foundation with provider-independent client and guardrails
- [x] **Task 23**: LLM-powered natural-language request interpreter with ambiguity detection
- [x] **Task 24**: LLM negotiation advisor for multi-offer trade-off analysis and counter-offer generation
- [x] **Task 25**: LLM recovery advisor for failure diagnosis and root-cause analysis
- [x] **Task 26**: Unified LLM Orchestrator integrating all three reasoning modules
- [x] **Task 27**: Agent-LLM Bridge connecting the 9 deterministic agents to AI reasoning
- [x] **Task 28**: End-to-End AI + Web3 integration testing across 10 critical scenarios
- [x] **Task 29**: Multi-hospital coordination testing under concurrency and shared capacity limits
- [x] **Task 30**: Security and adversarial attack stress testing across 12 threat models
- [x] **Task 31**: Blockchain reliability, mempool delay, transaction replacement, and state reconciliation testing
- [x] **PR #3**: PostgreSQL (Neon serverless) connection pool migration with advisory locking and SQLite fallback
- [ ] Multi-party threshold signatures (ERC-7739 / Multisig coordination)
- [ ] Zero-Knowledge Proofs (ZK-SNARKs) for anonymous institution credential attestation
- [ ] Push notification dispatchers (Webhook / SMS) for on-call hospital coordinators
- [ ] Production FHIR (Fast Healthcare Interoperability Resources) HL7 EHR connector

---

## 📄 License & Attribution

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

Built with ❤️ for decentralized healthcare resilience, patient privacy, and autonomous coordination.
