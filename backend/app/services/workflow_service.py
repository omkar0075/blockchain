import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Union
from uuid import UUID

from ..agents.context import FORBIDDEN_SECURITY_KEYWORDS
from ..agents.monitoring_agent import DefaultBlockchainReader
from ..db import (
    append_workflow_transition,
    get_connection,
    get_workflow_record,
    get_workflow_transitions,
    list_workflow_records,
    save_workflow_record,
)
from ..workflows.reservation_workflow import ReservationWorkflowResult

logger = logging.getLogger("medway.services.workflow")


class WorkflowStorageError(Exception):
    """Raised when an unrecoverable database or persistence failure occurs."""
    pass


class WorkflowSecurityError(ValueError):
    """Raised when a persistence payload violates zero-secrets or zero-PII security invariants."""
    pass


class WorkflowStateError(ValueError):
    """Raised when an illegal workflow state transition or duplicate execution is attempted."""
    pass


def scan_for_secrets_and_pii(data: Any) -> None:
    """
    Recursively scans structured payloads, dictionary keys, and string contents
    for forbidden credentials, private keys, or patient PII.
    """
    if isinstance(data, dict):
        for k, v in data.items():
            k_lower = str(k).lower()
            if k_lower in FORBIDDEN_SECURITY_KEYWORDS:
                raise WorkflowSecurityError(
                    f"Security violation: forbidden key '{k}' detected in workflow payload"
                )
            scan_for_secrets_and_pii(v)
    elif isinstance(data, (list, tuple, set)):
        for item in data:
            scan_for_secrets_and_pii(item)
    elif isinstance(data, str):
        val_lower = data.lower()
        for forbidden in (
            "private_key",
            "secret_key",
            "mnemonic",
            "bearer ",
            "begin private key",
            "patient_name",
            "ssn",
            "mrn",
        ):
            if forbidden in val_lower:
                raise WorkflowSecurityError(
                    f"Security violation: forbidden pattern '{forbidden}' detected in workflow payload string"
                )


class WorkflowService:
    """
    Persistent Workflow State Service.

    Maintains a durable SQLite replica of multi-agent coordination workflows across
    backend restarts and system crashes.

    Architectural & Governance Invariants:
    1. Durable Local Replica: Database records store workflow history and state, but
       authoritative status for capacity locks and reservations resides on-chain.
    2. Zero Secrets & PII: Strictly rejects any private keys, signing secrets,
       or patient-identifying clinical data from entering persistence.
    3. Append-Only Audit Transitions: Every state transition is permanently recorded
       in an append-only timeline table.
    4. Anti-Duplicate Execution: Workflows already committed on-chain cannot be re-executed
       or duplicate-committed following restart.
    5. On-Chain Reconciliation: Interrupted or pending workflows are verified against
       the smart contract state before resuming operational tasks.
    """

    def __init__(self, conn_factory: Optional[Callable[[], sqlite3.Connection]] = None) -> None:
        self.conn_factory = conn_factory or get_connection

    def persist_workflow(self, workflow_result: ReservationWorkflowResult) -> None:
        """
        Persist complete workflow state snapshot and its transition history to SQLite.
        """
        # 1. Enforce zero secrets & PII invariant on raw payload
        dumped = workflow_result.model_dump(mode="json")
        scan_for_secrets_and_pii(dumped)

        wf_id_str = str(workflow_result.workflow_id)

        # 2. Extract structured search/query columns
        req = workflow_result.request
        sel_offer = workflow_result.selected_offer
        sel_prop = workflow_result.selected_proposal
        ver = workflow_result.verification
        onchain = workflow_result.onchain_result or {}

        record: dict[str, Any] = {
            "workflow_id": wf_id_str,
            "state": workflow_result.state,
            "requester_id": req.requester_institution_id if req else None,
            "resource_type": (
                req.resource_type.value if hasattr(req.resource_type, "value") else str(req.resource_type)
            ) if req else None,
            "quantity": req.quantity if req else None,
            "selected_provider_id": sel_offer.provider_id if sel_offer else None,
            "selected_offer": json.dumps(sel_offer.model_dump(mode="json")) if sel_offer else None,
            "selected_proposal": json.dumps(sel_prop.model_dump(mode="json")) if sel_prop else None,
            "verification_result": json.dumps(ver.model_dump(mode="json")) if ver else None,
            "approved_by": workflow_result.approved_by,
            "approved_at": workflow_result.approved_at.isoformat() if workflow_result.approved_at else None,
            "is_ready_for_approval": workflow_result.is_ready_for_approval,
            "is_committed": workflow_result.is_committed,
            "tx_hash": onchain.get("tx_hash"),
            "block_number": onchain.get("block_number"),
            "lock_id": onchain.get("lock_id"),
            "reservation_id": onchain.get("reservation_id"),
            "onchain_status": workflow_result.monitoring_status or onchain.get("status"),
            "raw_payload": workflow_result.model_dump_json(),
            "created_at": workflow_result.created_at.timestamp(),
            "updated_at": workflow_result.updated_at.timestamp(),
        }

        try:
            save_workflow_record(record)
        except sqlite3.Error as e:
            logger.error("Database failure while persisting workflow '%s': %s", wf_id_str, e)
            raise WorkflowStorageError(f"Database error persisting workflow '{wf_id_str}': {e}") from e

        # 3. Synchronize audit trail transitions to append-only workflow_transitions table
        try:
            existing_transitions = get_workflow_transitions(wf_id_str)
            existing_count = len(existing_transitions)
            trail = workflow_result.audit_trail

            if len(trail) > existing_count:
                for step in trail[existing_count:]:
                    scan_for_secrets_and_pii(step)
                    append_workflow_transition(
                        workflow_id=wf_id_str,
                        from_state=step.get("from_state"),
                        to_state=step.get("to_state") or step.get("state", "UNKNOWN"),
                        agent=step.get("agent", "System"),
                        details=str(step.get("details", "")),
                        timestamp=step.get("timestamp"),
                    )
        except sqlite3.Error as e:
            logger.error("Database failure recording transitions for workflow '%s': %s", wf_id_str, e)
            raise WorkflowStorageError(f"Database error writing transitions for '{wf_id_str}': {e}") from e

    def load_workflow(self, workflow_id: Union[UUID, str]) -> Optional[ReservationWorkflowResult]:
        """
        Reload a persisted workflow from the SQLite replica by workflow ID.
        """
        wf_id_str = str(workflow_id)
        try:
            record = get_workflow_record(wf_id_str)
        except sqlite3.Error as e:
            logger.error("Database failure while loading workflow '%s': %s", wf_id_str, e)
            raise WorkflowStorageError(f"Database error loading workflow '{wf_id_str}': {e}") from e

        if not record:
            return None

        try:
            raw_json = record.get("raw_payload", "{}")
            wf = ReservationWorkflowResult.model_validate_json(raw_json)
            return wf
        except Exception as e:
            logger.error("Failed to deserialize workflow record for '%s': %s", wf_id_str, e)
            raise WorkflowStorageError(f"Corrupted workflow record for '{wf_id_str}': {e}") from e

    def record_transition(
        self,
        workflow_id: Union[UUID, str],
        from_state: Optional[str],
        to_state: str,
        agent: str,
        details: str,
        timestamp: Optional[float] = None,
    ) -> None:
        """
        Append an explicit state transition into the append-only transition history.
        """
        wf_id_str = str(workflow_id)
        scan_for_secrets_and_pii({"details": details, "agent": agent})

        ts = timestamp if timestamp is not None else time.time()
        try:
            append_workflow_transition(
                workflow_id=wf_id_str,
                from_state=from_state,
                to_state=to_state,
                agent=agent,
                details=details,
                timestamp=ts,
            )
            # Update state in main workflows table if present
            wf = self.load_workflow(workflow_id)
            if wf:
                wf.state = to_state
                wf.updated_at = datetime.fromtimestamp(ts, tz=timezone.utc)
                self.persist_workflow(wf)
        except sqlite3.Error as e:
            logger.error("Database failure while recording transition for '%s': %s", wf_id_str, e)
            raise WorkflowStorageError(f"Database error recording transition for '{wf_id_str}': {e}") from e

    def get_transition_history(self, workflow_id: Union[UUID, str]) -> list[dict[str, Any]]:
        """
        Retrieve chronological append-only state transition records.
        """
        wf_id_str = str(workflow_id)
        try:
            return get_workflow_transitions(wf_id_str)
        except sqlite3.Error as e:
            raise WorkflowStorageError(f"Database error reading transitions for '{wf_id_str}': {e}") from e

    def list_interrupted_workflows(self) -> list[ReservationWorkflowResult]:
        """
        List active workflows that were interrupted by a restart or crash.
        Excludes completed/terminal workflows.
        """
        terminal_states = {"CONFIRMED", "COMMITTED", "FULFILLED", "CANCELLED", "EXPIRED", "ESCALATED", "FAILED"}
        try:
            records = list_workflow_records()
        except sqlite3.Error as e:
            raise WorkflowStorageError(f"Database error listing workflows: {e}") from e

        interrupted: list[ReservationWorkflowResult] = []
        for r in records:
            if r.get("state") not in terminal_states:
                try:
                    wf = ReservationWorkflowResult.model_validate_json(r["raw_payload"])
                    interrupted.append(wf)
                except Exception as e:
                    logger.warning("Skipping unparseable workflow '%s': %s", r.get("workflow_id"), e)
        return interrupted

    def resume_workflow(
        self,
        workflow_id: Union[UUID, str],
        coordinator: Optional[Any] = None,
    ) -> ReservationWorkflowResult:
        """
        Restore an interrupted workflow into coordinator memory following backend restart.

        Guarantees:
        - Prevents duplicate execution if already committed.
        - Restores pending approvals safely into active memory.
        - Appends a RESUMED transition to the audit log.
        """
        wf = self.load_workflow(workflow_id)
        if not wf:
            raise KeyError(f"Workflow '{workflow_id}' not found in persistent storage")

        # Invariant 4: Prevent duplicate execution after restart
        if wf.is_committed:
            raise WorkflowStateError(
                f"Governance violation: Workflow '{workflow_id}' has already been committed to the blockchain. "
                "Duplicate on-chain execution is strictly prevented."
            )

        # Restore into active coordinator memory if coordinator is provided
        if coordinator is not None:
            coordinator._workflows[wf.workflow_id] = wf
            if wf.workflow_id in coordinator._active_runs:
                coordinator._active_runs.remove(wf.workflow_id)

        # Record resume transition
        self.record_transition(
            workflow_id=wf.workflow_id,
            from_state=wf.state,
            to_state=wf.state,
            agent="WorkflowService",
            details=f"Workflow restored into active coordinator memory across backend restart in state '{wf.state}'",
        )

        logger.info("Resumed workflow '%s' in state '%s'", wf.workflow_id, wf.state)
        return wf

    def reconcile_with_onchain(
        self,
        workflow_id: Union[UUID, str],
        reader: Optional[Any] = None,
    ) -> ReservationWorkflowResult:
        """
        Reconcile local database replica state against the authoritative smart contract state.

        The SQLite database is a durable local replica, not authoritative proof of an on-chain commitment;
        reconciliation against the smart contract is required before resuming on-chain operations.
        """
        wf = self.load_workflow(workflow_id)
        if not wf:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        onchain = wf.onchain_result or {}
        tx_hash = onchain.get("tx_hash")
        lock_id = onchain.get("lock_id")
        reservation_id = onchain.get("reservation_id")

        if not tx_hash and not lock_id and not reservation_id:
            # Pre-blockchain execution; local state is current
            return wf

        blockchain_reader = reader or DefaultBlockchainReader()
        old_state = wf.state

        # 1. Query transaction receipt
        if tx_hash:
            receipt = blockchain_reader.get_transaction_receipt(tx_hash)
            if receipt:
                status = receipt.get("status")
                block_num = receipt.get("blockNumber", receipt.get("block_number"))
                if status == 1 or status == "0x1":
                    wf.monitoring_status = "CONFIRMED"
                    wf.is_committed = True
                    wf.state = "CONFIRMED"
                    onchain["status"] = "CONFIRMED"
                    if block_num:
                        onchain["block_number"] = block_num
                    wf.onchain_result = onchain
                    self.record_transition(
                        workflow_id=wf.workflow_id,
                        from_state=old_state,
                        to_state="CONFIRMED",
                        agent="WorkflowService",
                        details=f"Authoritative on-chain reconciliation: confirmed in block {block_num}",
                    )
                elif status == 0 or status == "0x0":
                    wf.monitoring_status = "REVERTED"
                    wf.is_committed = False
                    wf.state = "REVERTED"
                    wf.error = "Transaction reverted on blockchain"
                    onchain["status"] = "REVERTED"
                    wf.onchain_result = onchain
                    self.record_transition(
                        workflow_id=wf.workflow_id,
                        from_state=old_state,
                        to_state="REVERTED",
                        agent="WorkflowService",
                        details="Authoritative on-chain reconciliation: transaction reverted on-chain",
                    )
            else:
                wf.monitoring_status = "PENDING"

        # 2. Query lock status if reader supports get_capacity_lock or get_lock
        if lock_id:
            lock_info = None
            if hasattr(blockchain_reader, "get_lock"):
                lock_info = blockchain_reader.get_lock(lock_id)
            elif hasattr(blockchain_reader, "get_capacity_lock"):
                lock_info = blockchain_reader.get_capacity_lock(lock_id)

            if lock_info:
                st = str(lock_info.get("status", "")).upper()
                if lock_info.get("released") or st in ("RELEASED", "CANCELLED"):
                    wf.state = "CANCELLED"
                    wf.monitoring_status = "RELEASED"
                elif lock_info.get("is_expired") or st == "EXPIRED":
                    wf.state = "EXPIRED"
                    wf.monitoring_status = "Expired"

        # 3. Query reservation status if reader supports get_reservation
        if reservation_id and hasattr(blockchain_reader, "get_reservation"):
            res_info = blockchain_reader.get_reservation(reservation_id)
            if res_info:
                st = str(res_info.get("status", "")).upper()
                if st in ("COMMITTED", "CONFIRMED"):
                    wf.is_committed = True
                    wf.monitoring_status = "Committed"
                elif st in ("FULFILLED", "COMPLETED"):
                    wf.monitoring_status = "Fulfilled"

        now = datetime.now(timezone.utc)
        wf.updated_at = now
        self.persist_workflow(wf)
        return wf
