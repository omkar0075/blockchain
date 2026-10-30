"""PostgreSQL Database Layer for MEDway with Connection Pooling, Advisory Locks, and Append-Only Audit Integrity."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

try:
    from dotenv import load_dotenv
    # Load backend/.env or root .env
    _backend_env = Path(__file__).resolve().parent.parent / ".env"
    _root_env = Path(__file__).resolve().parent.parent.parent / ".env"
    if _backend_env.exists():
        load_dotenv(_backend_env)
    elif _root_env.exists():
        load_dotenv(_root_env)
    else:
        load_dotenv()
except ImportError:
    pass

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    HAS_PSYCOPG = True
except ImportError:
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore
    ConnectionPool = None  # type: ignore
    HAS_PSYCOPG = False

logger = logging.getLogger("medway.db")

# Deterministic advisory lock keys for 64-bit PostgreSQL pg_advisory_xact_lock
# MEDWAY_AUDIT_LOG_LOCK_KEY protects the sequential hash chain across concurrent writes
AUDIT_LOG_ADVISORY_LOCK_ID: int = 0x4D454457415901  # "MEDWAY\x01"

# Database Configuration
DATABASE_URL = os.getenv("DATABASE_URL")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "medway")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "medway_secure_pass")
POSTGRES_DB = os.getenv("POSTGRES_DB", "medway_db")
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "20"))
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "30.0"))

# SQLite Fallback Paths (used if PostgreSQL is not configured or in fallback mode)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SQLITE_DB_PATH = DATA_DIR / "medway.db"

_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()
_active_backend: str = "sqlite"  # "postgresql" or "sqlite"


def get_active_backend() -> str:
    """Return the currently active database backend."""
    return _active_backend


def _build_postgres_conninfo() -> str:
    if DATABASE_URL:
        return DATABASE_URL
    return f"host={POSTGRES_HOST} port={POSTGRES_PORT} dbname={POSTGRES_DB} user={POSTGRES_USER} password={POSTGRES_PASSWORD}"


def init_pool(min_size: Optional[int] = None, max_size: Optional[int] = None, timeout: Optional[float] = None) -> Optional[ConnectionPool]:
    """Initialize PostgreSQL psycopg_pool connection pool."""
    global _pool, _active_backend
    if not HAS_PSYCOPG:
        logger.info("psycopg/psycopg_pool not available; defaulting to SQLite.")
        _active_backend = "sqlite"
        return None

    with _pool_lock:
        if _pool is not None:
            return _pool

        # If PostgreSQL configuration/intent is not explicitly provided, default seamlessly to local fallback
        if not (os.getenv("DATABASE_URL") or os.getenv("USE_POSTGRES") or os.getenv("POSTGRES_ENABLED")):
            logger.info("DATABASE_URL / USE_POSTGRES not specified. Defaulting to persistent storage mode.")
            _active_backend = "sqlite"
            return None

        conninfo = _build_postgres_conninfo()
        min_sz = min_size or DB_POOL_MIN_SIZE
        max_sz = max_size or DB_POOL_MAX_SIZE
        tout = timeout or DB_POOL_TIMEOUT

        try:
            new_pool = ConnectionPool(
                conninfo=conninfo,
                min_size=min_sz,
                max_size=max_sz,
                timeout=tout,
                kwargs={"row_factory": dict_row, "autocommit": False},
                open=False,
            )
            # Cloud databases (like Neon TLS handshakes) require a generous initial connection timeout
            open_timeout = float(os.getenv("DB_CONNECT_TIMEOUT", "10.0"))
            new_pool.open(wait=True, timeout=open_timeout)
            _pool = new_pool
            _active_backend = "postgresql"
            logger.info("Initialized PostgreSQL connection pool successfully.")
            return _pool
        except Exception as e:
            logger.warning("PostgreSQL connection failed (%s). Falling back to SQLite.", e)
            _active_backend = "sqlite"
            return None



def close_pool() -> None:
    """Close PostgreSQL connection pool gracefully."""
    global _pool, _active_backend
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
                logger.info("PostgreSQL connection pool closed.")
            except Exception as e:
                logger.error("Error closing PostgreSQL connection pool: %s", e)
            finally:
                _pool = None


class ConnectionWrapper:
    """Unified wrapper around psycopg.Connection or sqlite3.Connection with row dict access."""

    def __init__(self, raw_conn: Any, is_pg: bool = False):
        self.raw_conn = raw_conn
        self.is_pg = is_pg

    def _convert_query(self, query: str) -> str:
        """Convert SQLite-style '?' placeholders into PostgreSQL-style '%s' when on Postgres."""
        if not self.is_pg:
            return query
        # Transform '?' to '%s' outside quotes
        parts = []
        in_single = False
        in_double = False
        for char in query:
            if char == "'" and not in_double:
                in_single = not in_single
                parts.append(char)
            elif char == '"' and not in_single:
                in_double = not in_double
                parts.append(char)
            elif char == "?" and not in_single and not in_double:
                parts.append("%s")
            else:
                parts.append(char)
        return "".join(parts)

    def execute(self, query: str, params: Optional[Any] = None) -> CursorWrapper:
        adapted_query = self._convert_query(query)
        if self.is_pg:
            cur = self.raw_conn.cursor()
            if params is not None:
                cur.execute(adapted_query, params)
            else:
                cur.execute(adapted_query)
            return CursorWrapper(cur, is_pg=True)
        else:
            cur = self.raw_conn.cursor()
            if params is not None:
                cur.execute(adapted_query, params)
            else:
                cur.execute(adapted_query)
            return CursorWrapper(cur, is_pg=False)

    def commit(self) -> None:
        self.raw_conn.commit()

    def rollback(self) -> None:
        self.raw_conn.rollback()


class CursorWrapper:
    """Unified cursor wrapper."""

    def __init__(self, raw_cursor: Any, is_pg: bool = False):
        self.raw_cursor = raw_cursor
        self.is_pg = is_pg

    def fetchone(self) -> Optional[dict[str, Any]]:
        row = self.raw_cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = self.raw_cursor.fetchall()
        return [dict(r) for r in rows]

    @property
    def rowcount(self) -> int:
        return self.raw_cursor.rowcount


@contextmanager
def get_db_connection() -> Generator[ConnectionWrapper, None, None]:
    """Acquire a managed database connection from PostgreSQL pool or SQLite fallback."""
    global _pool, _active_backend
    if _pool is not None and _active_backend == "postgresql":
        with _pool.connection() as pg_conn:
            wrapper = ConnectionWrapper(pg_conn, is_pg=True)
            try:
                yield wrapper
                pg_conn.commit()
            except Exception:
                pg_conn.rollback()
                raise
    else:
        # SQLite connection
        conn = sqlite3.connect(str(SQLITE_DB_PATH), timeout=30.0)
        conn.row_factory = sqlite3.Row
        wrapper = ConnectionWrapper(conn, is_pg=False)
        try:
            yield wrapper
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def get_connection() -> Generator[ConnectionWrapper, None, None]:
    """Compatibility alias for get_db_connection()."""
    with get_db_connection() as conn:
        yield conn


def init_db() -> None:
    """Initialize schema, append-only trigger, and indexes on PostgreSQL or SQLite."""
    global _pool, _active_backend

    # Try PostgreSQL first if pool not yet initialized
    if _pool is None and HAS_PSYCOPG:
        init_pool()

    with get_db_connection() as conn:
        if conn.is_pg:
            _init_postgres_schema(conn)
        else:
            _init_sqlite_schema(conn)


def _init_postgres_schema(conn: ConnectionWrapper) -> None:
    """Create PostgreSQL tables, append-only security trigger function, and advisory lock structures."""
    # 1. Proposals table
    conn.execute(
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
            lock_id TEXT,
            raw_payload JSONB NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
        """
    )

    # 2. Commitments table
    conn.execute(
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
            terms_hash TEXT,
            canonical_terms TEXT,
            lock_id TEXT,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL
        )
        """
    )

    # 3. Evidence Vault
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_vault (
            id BIGSERIAL PRIMARY KEY,
            commitment_id TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            raw_document TEXT NOT NULL,
            computed_sha256 TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            timestamp DOUBLE PRECISION NOT NULL
        )
        """
    )

    # 4. Audit Log Table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id BIGSERIAL PRIMARY KEY,
            commitment_id TEXT,
            actor TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            details_json TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            timestamp DOUBLE PRECISION NOT NULL
        )
        """
    )

    # 5. Security Trigger: medway_audit_append_only
    # Ensures historical audit logs can NEVER be updated or deleted by any user or application query.
    conn.execute(
        """
        CREATE OR REPLACE FUNCTION medway_audit_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Security Violation: audit_log is strictly append-only. UPDATE and DELETE operations are forbidden.';
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    conn.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_medway_audit_append_only'
            ) THEN
                CREATE TRIGGER trg_medway_audit_append_only
                BEFORE UPDATE OR DELETE ON audit_log
                FOR EACH ROW
                EXECUTE FUNCTION medway_audit_append_only();
            END IF;
        END $$;
        """
    )

    # Indexes for optimal concurrency and lookups
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commitments_created ON commitments(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_commitment ON audit_log(commitment_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_commitment ON evidence_vault(commitment_id)")
    conn.commit()


def _init_sqlite_schema(conn: ConnectionWrapper) -> None:
    """Initialize persistent SQLite tables with append-only trigger emulation."""
    # 1. Proposals table
    conn.execute(
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
            lock_id TEXT,
            raw_payload TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )

    # 2. Commitments table
    conn.execute(
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
            terms_hash TEXT,
            canonical_terms TEXT,
            lock_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )

    # 3. Evidence Vault Table
    conn.execute(
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

    # 4. Tamper-Evident Audit Log Table
    conn.execute(
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

    # SQLite append-only triggers for consistency in fallback mode
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'Security Violation: audit_log is strictly append-only. UPDATE operations are forbidden.');
        END;
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'Security Violation: audit_log is strictly append-only. DELETE operations are forbidden.');
        END;
        """
    )
    conn.commit()


# Initialize on import
init_db()


def save_proposal(proposal: dict[str, Any]) -> None:
    proposal_id = proposal["proposal_id"]
    req = proposal["request"]
    with get_connection() as conn:
        if conn.is_pg:
            conn.execute(
                """
                INSERT INTO proposals (
                    proposal_id, requesting_institution_id, resource_type,
                    quantity, urgency, required_by_hours, selected_provider_id,
                    selected_quantity, status, lock_id, raw_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (proposal_id) DO UPDATE SET
                    requesting_institution_id = EXCLUDED.requesting_institution_id,
                    resource_type = EXCLUDED.resource_type,
                    quantity = EXCLUDED.quantity,
                    urgency = EXCLUDED.urgency,
                    required_by_hours = EXCLUDED.required_by_hours,
                    selected_provider_id = EXCLUDED.selected_provider_id,
                    selected_quantity = EXCLUDED.selected_quantity,
                    status = EXCLUDED.status,
                    lock_id = EXCLUDED.lock_id,
                    raw_payload = EXCLUDED.raw_payload
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
                    proposal.get("lock_id"),
                    json.dumps(proposal),
                    time.time(),
                ),
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO proposals (
                    proposal_id, requesting_institution_id, resource_type,
                    quantity, urgency, required_by_hours, selected_provider_id,
                    selected_quantity, status, lock_id, raw_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    proposal.get("lock_id"),
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
            raw = row["raw_payload"]
            if isinstance(raw, dict):
                return raw
            return json.loads(raw)
    return None


def save_commitment(commitment: dict[str, Any]) -> None:
    commitment_id = commitment["commitment_id"]
    now = time.time()
    created_at = commitment.get("created_at") or now
    commitment["created_at"] = created_at

    canonical_terms = commitment.get("canonical_terms")
    if isinstance(canonical_terms, (dict, list)):
        canonical_terms_str = json.dumps(canonical_terms, sort_keys=True, separators=(",", ":"))
    else:
        canonical_terms_str = canonical_terms

    with get_connection() as conn:
        if conn.is_pg:
            conn.execute(
                """
                INSERT INTO commitments (
                    commitment_id, proposal_id, requesting_institution_id,
                    provider_id, resource_type, quantity, status, approved_by,
                    ledger, onchain_reservation_id, tx_hash, block_number,
                    onchain_status, onchain_status_code, fulfilled_at,
                    dispute_evidence, disputed_by, dispute_reason, dispute_tx_hash,
                    dispute_block, resolution_notes, resolution_evidence,
                    resolution_tx_hash, resolution_block, relayer_topup_tx,
                    note, message, terms_hash, canonical_terms, lock_id, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT (commitment_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    approved_by = EXCLUDED.approved_by,
                    ledger = EXCLUDED.ledger,
                    onchain_reservation_id = EXCLUDED.onchain_reservation_id,
                    tx_hash = EXCLUDED.tx_hash,
                    block_number = EXCLUDED.block_number,
                    onchain_status = EXCLUDED.onchain_status,
                    onchain_status_code = EXCLUDED.onchain_status_code,
                    fulfilled_at = EXCLUDED.fulfilled_at,
                    dispute_evidence = EXCLUDED.dispute_evidence,
                    disputed_by = EXCLUDED.disputed_by,
                    dispute_reason = EXCLUDED.dispute_reason,
                    dispute_tx_hash = EXCLUDED.dispute_tx_hash,
                    dispute_block = EXCLUDED.dispute_block,
                    resolution_notes = EXCLUDED.resolution_notes,
                    resolution_evidence = EXCLUDED.resolution_evidence,
                    resolution_tx_hash = EXCLUDED.resolution_tx_hash,
                    resolution_block = EXCLUDED.resolution_block,
                    relayer_topup_tx = EXCLUDED.relayer_topup_tx,
                    note = EXCLUDED.note,
                    message = EXCLUDED.message,
                    terms_hash = EXCLUDED.terms_hash,
                    canonical_terms = EXCLUDED.canonical_terms,
                    lock_id = EXCLUDED.lock_id,
                    updated_at = EXCLUDED.updated_at
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
                    commitment.get("terms_hash"),
                    canonical_terms_str,
                    commitment.get("lock_id"),
                    created_at,
                    now,
                ),
            )
        else:
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
                    note, message, terms_hash, canonical_terms, lock_id, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    commitment.get("terms_hash"),
                    canonical_terms_str,
                    commitment.get("lock_id"),
                    created_at,
                    now,
                ),
            )


def _format_commitment_row(row: dict[str, Any]) -> dict[str, Any]:
    c = dict(row)
    if c.get("canonical_terms") and isinstance(c["canonical_terms"], str):
        try:
            c["canonical_terms"] = json.loads(c["canonical_terms"])
        except Exception:
            pass
    return c


def get_commitment(commitment_id: str) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM commitments WHERE commitment_id = ?",
            (commitment_id,),
        ).fetchone()
        if row:
            return _format_commitment_row(row)
    return None


def list_commitments() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM commitments ORDER BY created_at DESC"
        ).fetchall()
        return [_format_commitment_row(r) for r in rows]


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
    """Append a cryptographic, hash-chained audit event to the permanent record with advisory lock protection."""
    now = time.time()
    with get_connection() as conn:
        # Transaction-level advisory lock on PostgreSQL to prevent race conditions during hash chaining
        if conn.is_pg:
            conn.execute("SELECT pg_advisory_xact_lock(?)", (AUDIT_LOG_ADVISORY_LOCK_ID,))

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
