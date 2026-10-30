import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("medswarm.blockchain")

BACKEND_DIR = Path(__file__).resolve().parent.parent
NODE_SCRIPT = BACKEND_DIR / "blockchain-service.js"


def _run_node_command(args: list[str], timeout_seconds: int = 45) -> dict[str, Any]:
    """Execute a command against the Node.js Viem blockchain service via subprocess."""
    full_cmd = ["node", str(NODE_SCRIPT)] + args
    try:
        proc = subprocess.run(
            full_cmd,
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if stdout:
            try:
                data = json.loads(stdout)
                return data
            except json.JSONDecodeError:
                pass

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
