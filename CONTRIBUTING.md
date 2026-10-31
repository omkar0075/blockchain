# Contributing to MEDway

Thank you for your interest in contributing to **MEDway**! We welcome contributions from healthcare software engineers, web3 developers, AI researchers, and security specialists.

---

## 🏛️ Code of Conduct
All contributors and maintainers are expected to adhere to our [Code of Conduct](CODE_OF_CONDUCT.md). Please report unacceptable behavior to the project maintainers.

---

## 🔄 Branching Strategy & Git Workflow

We follow standard **Trunk-Based / Feature-Branch Development**:
* `main`: Production-ready, fully verified branch. All tests must pass before merging.
* `develop`: Integration branch for upcoming releases.
* `feature/<feature-name>`: Dedicated branch for new features or agent capabilities.
* `fix/<bug-description>`: Bug fixes and security patches.

### Conventional Commits
Please use [Conventional Commits](https://www.conventionalcommits.org/) format:
* `feat:` A new feature or agent module (e.g., `feat(llm): add streaming support for request interpreter`)
* `fix:` A bug fix (e.g., `fix(contracts): handle replay edge cases in 2pc capacity locks`)
* `docs:` Documentation improvements (e.g., `docs: update API reference and diagrams`)
* `test:` Adding or updating tests (e.g., `test(adversarial): add reentrancy fuzz test`)
* `refactor:` Code restructuring without changing behavior
* `perf:` Performance improvements or gas optimizations

---

## 🛠️ Local Development Setup

### 1. Prerequisites
- **Node.js**: v20.x or v22.x LTS
- **Python**: v3.11 or higher
- **Docker & Docker Compose** (Optional, for containerized local testing)
- **Git**

### 2. Environment Configuration
Clone the repository and initialize local environment files:
```bash
git clone https://github.com/omkar0075/blockchain.git
cd blockchain

cp .env.example backend/.env
cp .env.example contracts/.env
```

### 3. Install Dependencies
```bash
# Smart Contracts
cd contracts && npm install && cd ..

# Backend (Python + Node.js Bridge)
cd backend
npm install
python -m venv .venv
# Activate virtual environment
source .venv/bin/activate  # macOS / Linux
# or .\.venv\Scripts\Activate.ps1 # Windows
pip install -r requirements.txt
pip install pytest pytest-asyncio anyio httpx
cd ..

# Frontend
cd frontend && npm install && cd ..
```

---

## 🧪 Running the Verification & Test Suites

Before opening a pull request, ensure all verification suites pass:

```bash
# 1. Run Smart Contract Hardhat Suite (113 tests)
cd contracts
npx hardhat test

# 2. Run Backend Agent & LLM Pytest Suite (293 tests)
cd ../backend
pytest -v -k "not test_api"

# 3. Run System Verification Suite (8 modules)
python test-system-verification.py

# 4. Run Frontend Build
cd ../frontend
npm run build
```

---

## 🔒 Healthcare Privacy & Security Invariants

When contributing code, you must respect the following architectural invariants:
1. **Zero Protected Health Information (PHI) On-Chain**: Patient names, medical numbers, and clinical details must never enter smart contract parameters, blockchain transactions, or public logs.
2. **100% Gasless Institutional Participation**: Requester and Provider institutions sign EIP-712 digests off-chain with 0 gas expense. The relayer sponsors network transactions.
3. **Controlled LLM Reasoning**: Large Language Models are strictly advisory. They have no direct access to private keys and cannot sign transactions or call smart contracts directly.
4. **Append-Only Hash-Chained Audit Trail**: All state transitions must maintain cryptographic continuity with their parent event hash.

---

## 📬 Submitting a Pull Request
1. Fork the repository and create your feature branch from `main`.
2. Ensure all tests pass cleanly.
3. Push your branch to GitHub and open a Pull Request using our [PR Template](.github/PULL_REQUEST_TEMPLATE.md).
4. Maintainers will review your PR and provide feedback.
