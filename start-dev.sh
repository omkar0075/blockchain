#!/usr/bin/env bash
set -e

echo "==========================================================="
echo " Starting MEDway Platform Development Services"
echo "==========================================================="

# Check environment files
if [ ! -f "backend/.env" ]; then
    echo "[!] backend/.env not found, copying from .env.example..."
    cp .env.example backend/.env
fi

if [ ! -f "contracts/.env" ]; then
    echo "[!] contracts/.env not found, copying from .env.example..."
    cp .env.example contracts/.env
fi

echo "[1/3] Checking Viem Bridge Status..."
node backend/blockchain-service.js status

echo "[2/3] Launching FastAPI Backend on http://localhost:8000..."
(cd backend && uvicorn app.main:app --reload --port 8000) &
BACKEND_PID=$!

echo "[3/3] Launching Vite Frontend on http://localhost:5173..."
(cd frontend && npm run dev) &
FRONTEND_PID=$!

cleanup() {
    echo ""
    echo "[*] Shutting down MEDway services..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    exit 0
}

trap cleanup INT TERM

wait

