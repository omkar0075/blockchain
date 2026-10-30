import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "medswarm.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize persistent SQLite tables for proposals, commitments, and immutable audit logs."""
    with get_connection() as conn:
        cursor = conn.cursor()

        # 1. Proposals table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                requesting_institution_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                urgency TEXT NOT NULL,
                required_by_hours INTEGER NOT NULL,
                selected_provider_id TEXT,
                selected_quantity INTEGER,
                status TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

        # 2. Commitments table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS commitments (
                commitment_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL,
                requesting_institution_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                status TEXT NOT NULL,
                approved_by TEXT NOT NULL,
                ledger TEXT NOT NULL,
                onchain_reservation_id TEXT,
                tx_hash TEXT,
                block_number INTEGER,
                onchain_status TEXT,
                onchain_status_code INTEGER,
                fulfilled_at TEXT,
                dispute_evidence TEXT,
                disputed_by TEXT,
                dispute_reason TEXT,
                dispute_tx_hash TEXT,
                dispute_block INTEGER,
                resolution_notes TEXT,
                resolution_evidence TEXT,
                resolution_tx_hash TEXT,
                resolution_block INTEGER,
                relayer_topup_tx TEXT,
                note TEXT,
                message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

        # 3. Immutable Tamper-Evident Audit Log Table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commitment_id TEXT,
                actor TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
            """
        )

        conn.commit()


# Initialize on import
init_db()


def save_proposal(proposal: dict[str, Any]) -> None:
    proposal_id = proposal["proposal_id"]
    req = proposal["request"]
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO proposals (
                proposal_id, requesting_institution_id, resource_type,
                quantity, urgency, required_by_hours, selected_provider_id,
                selected_quantity, status, raw_payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                req["requesting_institution_id"],
                req["resource_type"],
                req["quantity"],
                req["urgency"],
                req["required_by_hours"],
                proposal.get("selected_provider_id"),
                proposal.get("selected_quantity", 0),
                proposal.get("status", "proposed"),
                json.dumps(proposal),
                time.time(),
            ),
        )


def get_proposal(proposal_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT raw_payload FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row:
            return json.loads(row["raw_payload"])
    return None


def save_commitment(commitment: dict[str, Any]) -> None:
    commitment_id = commitment["commitment_id"]
    now = time.time()
    created_at = commitment.get("created_at") or now
    commitment["created_at"] = created_at

    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO commitments (
                commitment_id, proposal_id, requesting_institution_id,
                provider_id, resource_type, quantity, status, approved_by,
                ledger, onchain_reservation_id, tx_hash, block_number,
                onchain_status, onchain_status_code, fulfilled_at,
                dispute_evidence, disputed_by, dispute_reason, dispute_tx_hash,
                dispute_block, resolution_notes, resolution_evidence,
                resolution_tx_hash, resolution_block, relayer_topup_tx,
                note, message, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                commitment_id,
                commitment.get("proposal_id", ""),
                commitment.get("requesting_institution_id", ""),
                commitment.get("provider_id", ""),
                commitment.get("resource_type", "diagnostic_slot"),
                commitment.get("quantity", 0),
                commitment.get("status", "authorized"),
                commitment.get("approved_by", ""),
                commitment.get("ledger", "coral-testnet"),
                commitment.get("onchain_reservation_id"),
                commitment.get("tx_hash"),
                commitment.get("block_number"),
                commitment.get("onchain_status"),
                commitment.get("onchain_status_code"),
                commitment.get("fulfilled_at"),
                commitment.get("dispute_evidence"),
                commitment.get("disputed_by"),
                commitment.get("dispute_reason"),
                commitment.get("dispute_tx_hash"),
                commitment.get("dispute_block"),
                commitment.get("resolution_notes"),
                commitment.get("resolution_evidence"),
                commitment.get("resolution_tx_hash"),
                commitment.get("resolution_block"),
                commitment.get("relayer_topup_tx"),
                commitment.get("note"),
                commitment.get("message"),
                created_at,
                now,
            ),
        )


def get_commitment(commitment_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM commitments WHERE commitment_id = ?",
            (commitment_id,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def list_commitments() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM commitments ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def record_audit_event(
    action: str,
    actor: str,
    role: str,
    details: dict[str, Any],
    commitment_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append a cryptographic, hash-chained audit event to the permanent record."""
    now = time.time()
    with get_connection() as conn:
        # 1. Fetch previous hash for chaining
        last_row = conn.execute(
            "SELECT event_hash FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev_hash = last_row["event_hash"] if last_row else "0x0000000000000000000000000000000000000000000000000000000000000000"

        details_str = json.dumps(details, sort_keys=True)
        raw_to_hash = f"{prev_hash}|{now}|{action}|{actor}|{role}|{commitment_id}|{details_str}".encode()
        event_hash = "0x" + hashlib.sha256(raw_to_hash).hexdigest()

        conn.execute(
            """
            INSERT INTO audit_log (
                commitment_id, actor, role, action, details_json,
                prev_hash, event_hash, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                commitment_id,
                actor,
                role,
                action,
                details_str,
                prev_hash,
                event_hash,
                now,
            ),
        )
        conn.commit()

        return {
            "action": action,
            "actor": actor,
            "role": role,
            "commitment_id": commitment_id,
            "prev_hash": prev_hash,
            "event_hash": event_hash,
            "timestamp": now,
            "details": details,
        }


def get_audit_trail(commitment_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    with get_connection() as conn:
        if commitment_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE commitment_id = ? ORDER BY id ASC LIMIT ?",
                (commitment_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        
        result = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d.pop("details_json"))
            result.append(d)
        return result
