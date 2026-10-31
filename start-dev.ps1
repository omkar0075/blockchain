# ==============================================================================
# MEDway One-Click Development Startup Script for Windows PowerShell
# ==============================================================================

Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host " Starting MEDway Platform Development Environment" -ForegroundColor Cyan
Write-Host "===========================================================" -ForegroundColor Cyan

# Ensure .env files exist
if (-not (Test-Path "backend\.env")) {
    Write-Host "[!] backend\.env not found, copying from .env.example..." -ForegroundColor Yellow
    Copy-Item ".env.example" -Destination "backend\.env"
}

if (-not (Test-Path "contracts\.env")) {
    Write-Host "[!] contracts\.env not found, copying from .env.example..." -ForegroundColor Yellow
    Copy-Item ".env.example" -Destination "contracts\.env"
}

Write-Host "`n[1/3] Testing Blockchain Viem Bridge..." -ForegroundColor Green
node backend/blockchain-service.js status

Write-Host "`n[2/3] Launching FastAPI Backend on http://127.0.0.1:8000..." -ForegroundColor Green
$backendProcess = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-Command", "cd backend; uvicorn app.main:app --reload --port 8000" -PassThru

Write-Host "`n[3/3] Launching Vite Frontend on http://localhost:5173..." -ForegroundColor Green
$frontendProcess = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-Command", "cd frontend; npm run dev" -PassThru

Write-Host "`n[✓] All MEDway services launched!" -ForegroundColor Green
Write-Host "    - Swagger API Docs:  http://127.0.0.1:8000/docs" -ForegroundColor Cyan
Write-Host "    - React Dashboard:   http://localhost:5173" -ForegroundColor Cyan
Write-Host "    - Coral Testnet RPC: https://coralrpc.mstblockchain.com" -ForegroundColor Cyan

