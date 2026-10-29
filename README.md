# MEDSWARM MVP Starter

A hackathon starter for the MEDSWARM healthcare resource-coordination prototype.

## MVP scope
- Three simulated institutions: Hospital A, Hospital B, Diagnostic Lab C
- One resource type: diagnostic appointment slots
- Resource discovery and basic constrained allocation
- Explicit authorization before a reservation is committed
- Simulated commitment ledger for the first milestone
- Blockchain adapter is intentionally a placeholder until MST Blockchain RPC, chain ID, EVM compatibility, and deployment instructions are verified

## Architecture
React dashboard -> FastAPI -> agent coordination + allocation -> PostgreSQL (later) / in-memory demo data
                                                       -> MST smart contract integration (after network verification)

## Run the backend
```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000/docs

## Run the frontend
Install Node.js LTS, then:
```bash
cd frontend
npm install
npm run dev
```
Open the URL printed by Vite.

## Important safety/design rules
- Demo data is synthetic; do not enter real patient data.
- Agents may recommend allocations but cannot independently bypass institutional authorization.
- The demo ledger is NOT a blockchain. Do not describe it as MST-integrated until the real network integration is implemented and tested.
- Before blockchain work, confirm MST EVM compatibility, RPC URL, chain ID, native gas token, testnet, and contract deployment process.
