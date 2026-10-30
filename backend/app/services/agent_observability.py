import json
import logging
import re
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Union
from uuid import UUID, uuid4
from pydantic import BaseModel, Field

from ..agents.context import FORBIDDEN_SECURITY_KEYWORDS
from ..db import (
    delete_observability_events,
    get_connection,
    get_observability_events,
    save_observability_event,
)

logger = logging.getLogger("medway.services.observability")

REDACTED_SECRET = "[REDACTED_SECRET]"
REDACTED_PII = "[REDACTED_PII]"

PATIENT_PII_KEYS = {
    "patient", "patient_id", "patient_name", "ssn", "mrn", "date_of_birth",
    "dob", "diagnosis", "clinical_notes", "prescriptions", "blood_type",
    "patient_address", "phone_number", "email", "insurance_id",
}

CREDENTIAL_KEYS = {
    "private_key", "secret", "secret_key", "credential", "mnemonic",
    "password", "api_key", "auth_token", "bearer_token", "access_token",
}

PUBLIC_BLOCKCHAIN_KEYS = {
    "tx_hash", "transaction_hash", "lock_id", "reservation_id",
    "block_hash", "contract_address", "address", "to_address", "from_address",
    "wallet", "wallet_address",
}

PII_REGEX_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), REDACTED_PII),
    (re.compile(r"\bMRN-[A-Za-z0-9]+\b", re.IGNORECASE), REDACTED_PII),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), REDACTED_PII),
    (re.compile(r"\b(?:\+?1[-.]?)?\(?[2-9]\d{2}\)?[-.]?\d{3}[-.]?\d{4}\b"), REDACTED_PII),
]

SECRET_REGEX_PATTERNS = [
    (re.compile(r"\b0x[a-fA-F0-9]{64}\b"), REDACTED_SECRET),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]+"), f"Bearer {REDACTED_SECRET}"),
    (re.compile(r"(?i)(private_key|secret_key|api_key|password)\s*[:=]\s*\S+"), rf"\1: {REDACTED_SECRET}"),
]


def redact_string(val: str) -> str:
    """Sanitize string values by masking detected secrets and patient PII."""
    for pattern, repl in SECRET_REGEX_PATTERNS:
        val = pattern.sub(repl, val)
    for pattern, repl in PII_REGEX_PATTERNS:
        val = pattern.sub(repl, val)
    return val


def redact_payload(data: Any, key_name: Optional[str] = None) -> Any:
    """
    Recursively sanitize dictionaries, lists, and string payloads.
    Guarantees secrets and patient-sensitive PII are never persisted in observability logs
    while preserving non-secret public blockchain transaction hashes and identifiers.
    """
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if k_lower in CREDENTIAL_KEYS:
                cleaned[k] = REDACTED_SECRET
            elif k_lower in PATIENT_PII_KEYS or k_lower in FORBIDDEN_SECURITY_KEYWORDS:
                cleaned[k] = REDACTED_PII
            else:
                cleaned[k] = redact_payload(v, key_name=k_lower)
        return cleaned
    elif isinstance(data, list):
        return [redact_payload(item, key_name=key_name) for item in data]
    elif isinstance(data, tuple):
        return tuple(redact_payload(item, key_name=key_name) for item in data)
    elif isinstance(data, set):
        return {redact_payload(item, key_name=key_name) for item in data}
    elif isinstance(data, str):
        if key_name in PUBLIC_BLOCKCHAIN_KEYS:
            return data
        return redact_string(data)
    else:
        return data


class ObservabilityEvent(BaseModel):
    """Structured telemetry event capturing agent and coordination lifecycle steps."""
    event_id: UUID = Field(default_factory=uuid4)
    workflow_id: str
    agent_id: str
    event_type: str
    status: str
    duration_ms: Optional[float] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)


class AgentObservabilityService:
    """
    Agent Observability Service.

    Provides deep, correlated visibility into multi-agent execution, explaining:
    - What every agent executed and how long it took.
    - Exactly why a specific provider was selected or rejected.
    - Verification outcomes and institutional human approval checkpoints.
    - On-chain capacity locking and transaction lifecycles.
    - Root-cause errors, recovery attempts, and failure classifications.

    Architectural Invariants:
    1. Passive Observability: Observability explains system behavior without becoming
       an authority. It must NEVER modify capacity, approvals, verification outcomes,
       or blockchain state.
    2. Zero Leakage: All captured payloads are actively redacted for secrets and patient PII.
    3. Separate Audit Stream: Stored in dedicated structured tables distinct from stdout/debug logs.
    4. Fault Tolerant: Observability storage errors are logged but NEVER crash the core workflow.
    """

    def __init__(self, conn_factory: Optional[Callable] = None) -> None:
        self.conn_factory = conn_factory or get_connection

    def record_event(
        self,
        workflow_id: Union[UUID, str],
        agent_id: str,
        event_type: str,
        status: str = "SUCCESS",
        duration_ms: Optional[float] = None,
        payload: Optional[dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Record a structured telemetry event.
        Automatically redacts secrets and patient PII, and handles storage failures safely.
        """
        wf_str = str(workflow_id)
        evt_id = str(uuid4())
        ts = timestamp if timestamp is not None else time.time()

        # Sanitize payload
        clean_payload = redact_payload(payload or {})

        event_record = {
            "event_id": evt_id,
            "workflow_id": wf_str,
            "agent_id": agent_id,
            "event_type": event_type,
            "status": status,
            "duration_ms": duration_ms,
            "payload": clean_payload,
            "payload_json": json.dumps(clean_payload),
            "timestamp": ts,
        }

        # Safe storage: never allow telemetry storage failures to disrupt agent execution
        try:
            save_observability_event(event_record)
        except Exception as e:
            logger.error(
                "Observability storage failure (workflow=%s, agent=%s, event=%s): %s",
                wf_str,
                agent_id,
                event_type,
                e,
            )
            # Fault-tolerant: do not re-raise!

        return event_record

    @contextmanager
    def track_agent(
        self,
        workflow_id: Union[UUID, str],
        agent_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ):
        """
        Synchronous context manager tracking agent execution start, duration, completion, or failure.
        """
        meta = metadata or {}
        start_time = time.perf_counter()
        self.record_event(
            workflow_id=workflow_id,
            agent_id=agent_id,
            event_type="AGENT_START",
            status="STARTED",
            payload=meta,
        )

        status = "SUCCESS"
        error_msg = None
        try:
            yield
        except Exception as exc:
            status = "FAILED"
            error_msg = str(exc)
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            if status == "SUCCESS":
                self.record_event(
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    event_type="AGENT_COMPLETE",
                    status="SUCCESS",
                    duration_ms=round(duration_ms, 2),
                    payload=meta,
                )
            else:
                failure_payload = {**meta, "error": error_msg}
                self.record_event(
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    event_type="AGENT_FAILED",
                    status="FAILED",
                    duration_ms=round(duration_ms, 2),
                    payload=failure_payload,
                )

    @asynccontextmanager
    async def track_agent_async(
        self,
        workflow_id: Union[UUID, str],
        agent_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ):
        """
        Asynchronous context manager tracking agent execution start, duration, completion, or failure.
        """
        meta = metadata or {}
        start_time = time.perf_counter()
        self.record_event(
            workflow_id=workflow_id,
            agent_id=agent_id,
            event_type="AGENT_START",
            status="STARTED",
            payload=meta,
        )

        status = "SUCCESS"
        error_msg = None
        try:
            yield
        except Exception as exc:
            status = "FAILED"
            error_msg = str(exc)
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            if status == "SUCCESS":
                self.record_event(
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    event_type="AGENT_COMPLETE",
                    status="SUCCESS",
                    duration_ms=round(duration_ms, 2),
                    payload=meta,
                )
            else:
                failure_payload = {**meta, "error": error_msg}
                self.record_event(
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    event_type="AGENT_FAILED",
                    status="FAILED",
                    duration_ms=round(duration_ms, 2),
                    payload=failure_payload,
                )

    def record_decision(
        self,
        workflow_id: Union[UUID, str],
        agent_id: str,
        decision_type: str,
        selected_option: str,
        rationale: str,
        evaluated_candidates: Optional[list[Any]] = None,
        criteria: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Record a deterministic decision (e.g. why a specific provider or offer was selected).
        """
        payload = {
            "decision_type": decision_type,
            "selected_option": selected_option,
            "rationale": rationale,
            "evaluated_candidates": evaluated_candidates or [],
            "criteria": criteria or {},
        }
        self.record_event(
            workflow_id=workflow_id,
            agent_id=agent_id,
            event_type="OFFER_SELECTION" if "offer" in decision_type.lower() else "DECISION",
            status="SUCCESS",
            payload=payload,
        )

    def record_verification(
        self,
        workflow_id: Union[UUID, str],
        provider_id: str,
        status: str,
        passed_checks: Optional[list[str]] = None,
        failed_checks: Optional[list[dict[str, Any]]] = None,
        details: Optional[str] = None,
    ) -> None:
        """
        Record verification outcomes and individual checks.
        """
        payload = {
            "provider_id": provider_id,
            "verification_status": status,
            "passed_checks": passed_checks or [],
            "failed_checks": failed_checks or [],
            "details": details or "",
        }
        self.record_event(
            workflow_id=workflow_id,
            agent_id="VerificationAgent",
            event_type="VERIFICATION_OUTCOME",
            status="SUCCESS" if status.upper() == "VERIFIED" else "FAILED",
            payload=payload,
        )

    def record_approval(
        self,
        workflow_id: Union[UUID, str],
        approved_by: str,
        checkpoint_name: str = "HUMAN_APPROVAL",
        details: Optional[str] = None,
    ) -> None:
        """
        Record attributable institutional human approval checkpoints.
        """
        payload = {
            "checkpoint_name": checkpoint_name,
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "details": details or f"Authorized by {approved_by}",
        }
        self.record_event(
            workflow_id=workflow_id,
            agent_id="InstitutionalOfficer",
            event_type="APPROVAL_CHECKPOINT",
            status="SUCCESS",
            payload=payload,
        )

    def record_blockchain_transaction(
        self,
        workflow_id: Union[UUID, str],
        tx_hash: str,
        operation: str,
        status: str,
        block_number: Optional[int] = None,
        gas_used: Optional[int] = None,
        details: Optional[str] = None,
    ) -> None:
        """
        Record on-chain transaction lifecycle events.
        """
        payload = {
            "tx_hash": tx_hash,
            "operation": operation,
            "transaction_status": status,
            "block_number": block_number,
            "gas_used": gas_used,
            "details": details or "",
        }
        self.record_event(
            workflow_id=workflow_id,
            agent_id="BlockchainExecutionAgent",
            event_type="BLOCKCHAIN_TRANSACTION",
            status=status.upper(),
            payload=payload,
        )

    def record_failure_or_retry(
        self,
        workflow_id: Union[UUID, str],
        agent_id: str,
        error_message: str,
        classification: Optional[str] = None,
        attempt: int = 1,
        max_attempts: int = 3,
        is_recoverable: bool = True,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Record structured failures, recovery classifications, and retry attempts.
        """
        payload = {
            "error_message": error_message,
            "classification": str(classification) if classification else "UNKNOWN",
            "attempt": attempt,
            "max_attempts": max_attempts,
            "is_recoverable": is_recoverable,
            **(details or {}),
        }
        self.record_event(
            workflow_id=workflow_id,
            agent_id=agent_id,
            event_type="RETRY_ATTEMPT" if attempt > 1 else "FAILURE",
            status="FAILED" if not is_recoverable else "RETRYING",
            payload=payload,
        )

    def get_events(
        self,
        workflow_id: Optional[Union[UUID, str]] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """
        Retrieve structured observability events matching query criteria.
        """
        try:
            wf_str = str(workflow_id) if workflow_id else None
            return get_observability_events(
                workflow_id=wf_str,
                agent_id=agent_id,
                event_type=event_type,
                limit=limit,
            )
        except Exception as e:
            logger.error("Failed to query observability events: %s", e)
            return []

    def get_workflow_summary(self, workflow_id: Union[UUID, str]) -> dict[str, Any]:
        """
        Generate a comprehensive, human-readable execution summary for a workflow.
        Explains:
        - Total duration and lifecycle events.
        - Which agents participated.
        - Why a provider was chosen.
        - Verification and human approval checkpoints.
        - On-chain transaction status.
        - Failures and retries.
        """
        events = self.get_events(workflow_id=workflow_id, limit=5000)
        wf_str = str(workflow_id)

        if not events:
            return {
                "workflow_id": wf_str,
                "status": "NO_EVENTS_FOUND",
                "total_events": 0,
                "total_duration_ms": 0.0,
                "agents_involved": [],
                "decisions": [],
                "checkpoints": [],
                "blockchain_transactions": [],
                "errors_and_retries": [],
                "timeline": [],
            }

        start_ts = events[0]["timestamp"]
        end_ts = events[-1]["timestamp"]
        total_duration_ms = round((end_ts - start_ts) * 1000.0, 2)

        agents_set: list[str] = []
        for e in events:
            aid = e.get("agent_id")
            if aid and aid not in agents_set:
                agents_set.append(aid)

        decisions: list[dict[str, Any]] = []
        checkpoints: list[dict[str, Any]] = []
        blockchain_txs: list[dict[str, Any]] = []
        errors_and_retries: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []

        overall_status = "IN_PROGRESS"

        for e in events:
            etype = e.get("event_type")
            status = e.get("status")
            payload = e.get("payload", {})

            timeline.append({
                "timestamp": e.get("timestamp"),
                "agent_id": e.get("agent_id"),
                "event_type": etype,
                "status": status,
                "duration_ms": e.get("duration_ms"),
                "details": payload,
            })

            if etype in ("DECISION", "OFFER_SELECTION"):
                decisions.append({
                    "agent": e.get("agent_id"),
                    "decision": payload.get("decision_type"),
                    "selected": payload.get("selected_option"),
                    "rationale": payload.get("rationale"),
                })
            elif etype in ("VERIFICATION_OUTCOME", "APPROVAL_CHECKPOINT"):
                checkpoints.append({
                    "type": etype,
                    "agent": e.get("agent_id"),
                    "status": status,
                    "payload": payload,
                })
            elif etype == "BLOCKCHAIN_TRANSACTION":
                blockchain_txs.append(payload)
                if status in ("CONFIRMED", "SUCCESS"):
                    overall_status = "CONFIRMED"
                elif status in ("REVERTED", "FAILED"):
                    overall_status = "FAILED"
            elif etype in ("FAILURE", "RETRY_ATTEMPT", "AGENT_FAILED"):
                errors_and_retries.append({
                    "agent": e.get("agent_id"),
                    "event_type": etype,
                    "status": status,
                    "error": payload.get("error") or payload.get("error_message"),
                    "attempt": payload.get("attempt"),
                })

        # Check last event for status
        last_event = events[-1]
        if overall_status == "IN_PROGRESS":
            if last_event.get("event_type") == "APPROVAL_CHECKPOINT":
                overall_status = "APPROVED"
            elif last_event.get("event_type") == "VERIFICATION_OUTCOME" and last_event.get("status") == "SUCCESS":
                overall_status = "WAITING_FOR_APPROVAL"
            elif last_event.get("status") == "FAILED":
                overall_status = "FAILED"

        return {
            "workflow_id": wf_str,
            "status": overall_status,
            "total_events": len(events),
            "total_duration_ms": total_duration_ms,
            "agents_involved": agents_set,
            "decisions": decisions,
            "checkpoints": checkpoints,
            "blockchain_transactions": blockchain_txs,
            "errors_and_retries": errors_and_retries,
            "timeline": timeline,
        }

    def clear_events(self, workflow_id: Optional[Union[UUID, str]] = None) -> None:
        """Clear recorded observability events."""
        try:
            delete_observability_events(str(workflow_id) if workflow_id else None)
        except Exception as e:
            logger.error("Failed to clear observability events: %s", e)
