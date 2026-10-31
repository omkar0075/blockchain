# ==============================================================================
# MEDway Platform Makefile
# ==============================================================================

.PHONY: help install test test-contracts test-backend test-system dev dev-backend dev-frontend docker-up docker-down clean

help:
	@echo "MEDway Autonomous Healthcare Coordination Platform"
	@echo "--------------------------------------------------"
	@echo "Available commands:"
	@echo "  make install         Install all dependencies (contracts, backend, frontend)"
	@echo "  make test            Run all automated test suites (contracts + backend)"
	@echo "  make test-contracts  Run Hardhat Solidity test suite (113 tests)"
	@echo "  make test-backend    Run Pytest multi-agent & LLM test suite (293 tests)"
	@echo "  make test-system     Run end-to-end system verification harness"
	@echo "  make dev-backend     Start FastAPI backend server on :8000"
	@echo "  make dev-frontend    Start Vite frontend dashboard on :5173"
	@echo "  make docker-up       Build and start all services via Docker Compose"
	@echo "  make docker-down     Stop all containerized services"
	@echo "  make clean           Clean up build artifacts, cache, and temporary files"

install:
	@echo "[*] Installing contract dependencies..."
	cd contracts && npm install
	@echo "[*] Installing backend dependencies..."
	cd backend && npm install && pip install -r requirements.txt
	@echo "[*] Installing frontend dependencies..."
	cd frontend && npm install

test: test-contracts test-backend test-system
	@echo "[✓] All MEDway test suites completed successfully!"

test-contracts:
	@echo "[*] Running Hardhat smart contract tests..."
	cd contracts && npx hardhat test

test-backend:
	@echo "[*] Running Pytest agent and LLM tests..."
	cd backend && pytest -v -k "not test_api"

test-system:
	@echo "[*] Running end-to-end system verification..."
	cd backend && python test-system-verification.py

dev-backend:
	cd backend && uvicorn app.main:app --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

clean:
	rm -rf contracts/cache contracts/artifacts contracts/typechain-types
	rm -rf backend/.pytest_cache backend/__pycache__ backend/app/__pycache__
	rm -rf frontend/dist

