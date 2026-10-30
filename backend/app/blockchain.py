import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("medway.blockchain")

BACKEND_DIR = Path(__file__).resolve().parent.parent
NODE_SCRIPT = BACKEND_DIR / "blockchain-service.js"

# Windows NTSTATUS 0xC000013A (STATUS_CONTROL_C_EXIT) in unsigned and signed 32-bit forms
INTERRUPT_EXIT_CODES = {3221225786, -1073741510, 0xC000013A, -2}


def _run_node_command(args: list[str], timeout_seconds: int = 45, max_retries: int = 1) -> dict[str, Any]:
    """Execute a command against the Node.js Viem blockchain service via subprocess."""
    full_cmd = ["node", str(NODE_SCRIPT)] + args
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # Detach child process from the parent console to prevent
        # server hot-reload (WatchFiles/uvicorn) signals from abruptly killing Node.js.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(
                full_cmd,
                cwd=str(BACKEND_DIR),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                **kwargs,
            )

            stdout = proc.stdout.strip()
            stderr = proc.stderr.strip()

            if stdout:
                try:
                    data = json.loads(stdout)
                    return data
                except json.JSONDecodeError:
                    pass

            # Detect Windows STATUS_CONTROL_C_EXIT caused by uvicorn/watchfiles hot-reload
            if proc.returncode in INTERRUPT_EXIT_CODES:
                if attempt < max_retries:
                    logger.warning("Blockchain subprocess interrupted during server reload; retrying once...")
                    continue
                return {
                    "success": False,
                    "error": "Blockchain service command interrupted by server reload or shutdown",
                    "interrupted": True,
                }

            if proc.returncode != 0:
                error_msg = stderr or stdout or f"Process exited with code {proc.returncode}"
                logger.error("Blockchain service subprocess error: %s", error_msg)
                return {"success": False, "error": error_msg}

            return {"success": False, "error": "Empty or non-JSON output from blockchain service"}

        except subprocess.TimeoutExpired:
            logger.error("Blockchain service timed out after %s seconds", timeout_seconds)
            return {"success": False, "error": f"Operation timed out after {timeout_seconds}s"}
        except Exception as e:
            logger.exception("Unexpected error executing blockchain service subprocess")
            return {"success": False, "error": str(e)}

    return {"success": False, "error": "Unknown blockchain execution failure"}


def get_blockchain_status() -> dict[str, Any]:
    """Retrieve Coral Testnet connection status, block height, and contract details."""
    return _run_node_command(["status"])


def create_onchain_reservation(
    requester: str,
    provider: str,
    resource_ref: str,
    expires_in: int = 7200,
) -> dict[str, Any]:
    """Submit an off-chain signed EIP-712 reservation proposal through the relayer on Coral."""
    return _run_node_command([
        "create",
        "--requester",
        requester,
        "--provider",
        provider,
        "--resourceRef",
        resource_ref,
        "--expiresIn",
        str(expires_in),
    ])


def accept_onchain_reservation(
    reservation_id: str,
    provider: str = "lab-c",
) -> dict[str, Any]:
    """Submit an off-chain signed EIP-712 provider acceptance through the relayer on Coral."""
    return _run_node_command([
        "accept",
        "--reservationId",
        reservation_id,
        "--provider",
        provider,
    ])


def get_onchain_reservation(reservation_id: str) -> dict[str, Any]:
    """Read full reservation state directly from Coral contract storage."""
    return _run_node_command([
        "get",
        "--reservationId",
        reservation_id,
    ])


def fulfill_onchain_reservation(
    reservation_id: str,
    provider: str = "lab-c",
) -> dict[str, Any]:
    """Attest resource delivery directly on Coral Testnet as provider with automated relayer gas top-up."""
    return _run_node_command([
        "fulfill",
        "--reservationId",
        reservation_id,
        "--provider",
        provider,
    ])


def dispute_onchain_reservation(
    reservation_id: str,
    actor: str = "hospital-a",
    reason: Optional[str] = None,
    evidence_hash: Optional[str] = None,
) -> dict[str, Any]:
    """Raise an on-chain dispute against a committed or fulfilled reservation."""
    cmd = [
        "dispute",
        "--reservationId",
        reservation_id,
        "--actor",
        actor,
    ]
    if evidence_hash:
        cmd.extend(["--evidenceHash", evidence_hash])
    if reason:
        cmd.extend(["--reason", reason])
    return _run_node_command(cmd)


def resolve_onchain_dispute(
    reservation_id: str,
    resolution: str = "Cancelled",
    notes: Optional[str] = None,
    resolution_evidence: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve an active dispute on-chain as consortium owner/arbitrator."""
    cmd = [
        "resolve",
        "--reservationId",
        reservation_id,
        "--resolution",
        resolution,
    ]
    if resolution_evidence:
        cmd.extend(["--resolutionEvidence", resolution_evidence])
    if notes:
        cmd.extend(["--notes", notes])
    return _run_node_command(cmd)


def check_institution_onchain(identifier: str) -> dict[str, Any]:
    """Verify institution accreditation and wallet role directly from Coral Testnet registry."""
    return _run_node_command([
        "check-institution",
        "--institution",
        identifier,
    ])


# -------------------------------------------------------------------
# Feature 3: Capacity Inventory & Temporary Locking
# -------------------------------------------------------------------


def set_onchain_inventory(
    provider: str,
    resource_type: str,
    total_capacity: int,
    is_active: bool = True,
) -> dict[str, Any]:
    """Set total capacity inventory for a provider on Coral Testnet."""
    return _run_node_command([
        "set-inventory",
        "--provider",
        provider,
        "--resourceType",
        resource_type,
        "--totalCapacity",
        str(total_capacity),
        "--isActive",
        str(is_active).lower(),
    ])


def get_onchain_capacity(
    provider: str,
    resource_type: str = "diagnostic_slot",
) -> dict[str, Any]:
    """Query real-time available capacity and full inventory directly from Coral Testnet."""
    return _run_node_command([
        "get-capacity",
        "--provider",
        provider,
        "--resourceType",
        resource_type,
    ])


def lock_onchain_capacity(
    requester: str,
    provider: str,
    resource_type: str = "diagnostic_slot",
    quantity: int = 1,
    duration: int = 1800,
) -> dict[str, Any]:
    """Execute gasless EIP-712 temporary capacity lock on Coral Testnet."""
    return _run_node_command([
        "lock-capacity",
        "--requester",
        requester,
        "--provider",
        provider,
        "--resourceType",
        resource_type,
        "--quantity",
        str(quantity),
        "--duration",
        str(duration),
    ])


def create_reservation_with_lock(
    lock_id: str,
    resource_ref: str,
    expires_in: int = 7200,
) -> dict[str, Any]:
    """Atomically convert an active capacity lock into an on-chain reservation commitment."""
    return _run_node_command([
        "create-reservation-with-lock",
        "--lockId",
        lock_id,
        "--resourceRef",
        resource_ref,
        "--expiresIn",
        str(expires_in),
    ])


def release_onchain_lock(
    lock_id: str,
    actor: str = "hospital-a",
    reason: str = "declined-by-coordinator",
) -> dict[str, Any]:
    """Release an uncommitted or expired capacity lock back to available inventory."""
    return _run_node_command([
        "release-lock",
        "--lockId",
        lock_id,
        "--actor",
        actor,
        "--reason",
        reason,
    ])


def get_onchain_lock(lock_id: str) -> dict[str, Any]:
    """Read capacity lock state directly from Coral contract storage."""
    return _run_node_command([
        "get-lock",
        "--lockId",
        lock_id,
    ])
