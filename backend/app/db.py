import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "medway.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize persistent SQLite tables for proposals, commitments, evidence vault, and audit logs."""
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

        # 3. Evidence Vault Table (stores off-chain raw clinical/operational documents)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_vault (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commitment_id TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                raw_document TEXT NOT NULL,
                computed_sha256 TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
            """
        )

        # 4. Immutable Tamper-Evident Audit Log Table
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


def store_evidence(
    commitment_id: str,
    evidence_type: str,
    raw_document: str,
    submitted_by: str,
) -> dict[str, Any]:
    """Store raw off-chain evidence and compute its cryptographic SHA-256 digest."""
    now = time.time()
    computed_sha256 = "0x" + hashlib.sha256(raw_document.encode()).hexdigest()

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO evidence_vault (
                commitment_id, evidence_type, raw_document,
                computed_sha256, submitted_by, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                commitment_id,
                evidence_type,
                raw_document,
                computed_sha256,
                submitted_by,
                now,
            ),
        )
        conn.commit()

    return {
        "commitment_id": commitment_id,
        "evidence_type": evidence_type,
        "raw_document": raw_document,
        "computed_sha256": computed_sha256,
        "submitted_by": submitted_by,
        "timestamp": now,
    }


def get_evidence_records(commitment_id: str) -> list[dict[str, Any]]:
    """Retrieve raw evidence records and independently recompute their SHA-256 hashes."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM evidence_vault WHERE commitment_id = ? ORDER BY id ASC",
            (commitment_id,),
        ).fetchall()

        results = []
        for r in rows:
            raw_doc = r["raw_document"]
            # Independently recompute hash to prove data has not been modified
            recomputed = "0x" + hashlib.sha256(raw_doc.encode()).hexdigest()
            results.append({
                "id": r["id"],
                "commitment_id": r["commitment_id"],
                "evidence_type": r["evidence_type"],
                "raw_document": raw_doc,
                "stored_sha256": r["computed_sha256"],
                "recomputed_sha256": recomputed,
                "integrity_verified": recomputed.lower() == r["computed_sha256"].lower(),
                "submitted_by": r["submitted_by"],
                "timestamp": r["timestamp"],
            })
        return results


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


def verify_audit_trail_integrity() -> dict[str, Any]:
    """Walk the complete audit log from root to tip, verifying mathematical hash-chain continuity."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id ASC").fetchall()
        if not rows:
            return {"valid": True, "total_records": 0, "message": "Audit log is empty"}

        expected_prev = "0x0000000000000000000000000000000000000000000000000000000000000000"
        for r in rows:
            # 1. Check prev_hash linkage
            if r["prev_hash"] != expected_prev:
                return {
                    "valid": False,
                    "broken_at_id": r["id"],
                    "error": "Broken chain linkage: prev_hash does not match previous event_hash",
                    "expected_prev": expected_prev,
                    "stored_prev": r["prev_hash"],
                }

            # 2. Recompute expected event_hash
            raw = f"{r['prev_hash']}|{r['timestamp']}|{r['action']}|{r['actor']}|{r['role']}|{r['commitment_id']}|{r['details_json']}".encode()
            computed_hash = "0x" + hashlib.sha256(raw).hexdigest()

            if r["event_hash"] != computed_hash:
                return {
                    "valid": False,
                    "broken_at_id": r["id"],
                    "error": "Tampered record detected: event_hash does not match recomputed SHA-256 digest",
                    "expected_hash": computed_hash,
                    "stored_hash": r["event_hash"],
                }

            expected_prev = r["event_hash"]

        return {
            "valid": True,
            "total_records": len(rows),
            "root_event_hash": expected_prev,
            "status": "Cryptographically verified & tamper-evident",
        }
