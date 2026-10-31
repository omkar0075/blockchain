from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Optional, Protocol, Union
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from .base import BaseAgent
from ..schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)

logger = logging.getLogger("medway.agents.monitoring")


class ReservationLifecycleState(str, Enum):
    """Authoritative lifecycle states of on-chain commitments and capacity locks."""
    PROPOSED = "PROPOSED"
    LOCKED = "LOCKED"
    COMMITTED = "COMMITTED"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    DISPUTED = "DISPUTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class BlockchainEvent(BaseModel):
    """Structured representation of an on-chain event emitted by smart contracts."""
    model_config = ConfigDict(extra="ignore")

    event_name: str = Field(..., min_length=1, description="Event name (e.g. CapacityLocked, ReservationCommitted)")
    transaction_hash: str = Field(..., min_length=1, description="Transaction hash emitting the event")
    block_number: Optional[int] = Field(default=None, ge=0, description="Block number where event was mined")
    log_index: Optional[int] = Field(default=0, ge=0, description="Log index within the block")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Event detection time")
    payload: dict[str, Any] = Field(default_factory=dict, description="Event parameters (e.g. reservationId, lockId, actor)")

    @property
    def event_key(self) -> str:
        """Unique event deduplication identifier."""
        return f"{self.event_name}:{self.transaction_hash.lower()}:{self.log_index or 0}"


class MonitoredEntity(BaseModel):
    """In-memory synchronized view of an on-chain lock or reservation."""
    model_config = ConfigDict(extra="ignore")

    entity_id: str
    entity_type: str  # "lock" or "reservation"
    workflow_id: Optional[UUID] = None
    state: ReservationLifecycleState = ReservationLifecycleState.PROPOSED
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    expires_at: Optional[datetime] = None
    fulfilled_at: Optional[datetime] = None
    dispute_evidence: Optional[str] = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    last_checked_at: Optional[datetime] = None


class BlockchainReaderProtocol(Protocol):
    """Protocol for read-only inspection of on-chain state and transaction receipts."""

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any]: ...
    def get_lock(self, lock_id: str) -> dict[str, Any]: ...
    def get_reservation(self, reservation_id: str) -> dict[str, Any]: ...


class DefaultBlockchainReader:
    """Default reader querying backend.blockchain functions."""

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        from ..blockchain import _run_node_command
        # Queries node service for transaction status
        return _run_node_command(["receipt", "--txHash", tx_hash])

    def get_lock(self, lock_id: str) -> dict[str, Any]:
        from ..blockchain import get_onchain_lock
        return get_onchain_lock(lock_id)

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        from ..blockchain import get_onchain_reservation
        return get_onchain_reservation(reservation_id)


class MonitoringAgent(BaseAgent):
    """
    Autonomous Monitoring Agent.

    Tracks transaction confirmation status, monitors on-chain commitment lifecycles,
    detects expired capacity locks and approaching deadlines, and listens for smart contract events.

    Architectural & Governance Invariants:
    1. Read-Only Observability: Strictly observative. Never submits transactions to mutate state.
    2. Authoritative Synchronization: On-chain contracts and events are the authoritative source
       of truth; local workflow views are synchronized replicas.
    3. Proof-Gated Fulfillment: A reservation is NEVER marked FULFILLED without authoritative
       on-chain fulfillment evidence (receipt or on-chain status verification).
    4. Event Deduplication: Tracks and ignores duplicate event transmissions.
    5. Proactive Deadline Protection: Detects expired locks and warns of approaching fulfillment windows.
    6. Audit Lineage: Preserves transaction hashes, block numbers, and state transitions linked to workflow_id.
    """

    def __init__(
        self,
        name: str = "MonitoringAgent",
        reader: Optional[Any] = None,
        deadline_warning_seconds: int = 1800,  # 30 minutes
        confirmation_timeout_seconds: int = 120,  # 2 minutes
        required_confirmations: int = 1,
    ) -> None:
        super().__init__(name=name)
        self.reader = reader or DefaultBlockchainReader()
        self.deadline_warning_seconds = deadline_warning_seconds
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self.required_confirmations = required_confirmations

        # In-memory tracking structures
        self._processed_event_keys: set[str] = set()
        self._entities: dict[str, MonitoredEntity] = {}
        self._workflow_mappings: dict[UUID, list[str]] = {}

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Process a monitoring, reconciliation, or event dispatch task.

        Supported message_type values:
            - CHECK_TRANSACTION: Verify receipt and confirmation of a tx_hash.
            - PROCESS_EVENT: Process and deduplicate an incoming BlockchainEvent.
            - RECONCILE_STATE: Reconcile entity state with on-chain contract storage.
            - CHECK_DEADLINES: Evaluate deadlines and expired locks across monitored entities.
        """
        m_type = message.message_type or "CHECK_TRANSACTION"
        payload = message.payload or {}
        wf_id = message.workflow_id

        if m_type == "CHECK_TRANSACTION":
            return await self._check_transaction(payload, wf_id)
        elif m_type == "PROCESS_EVENT":
            return await self._process_blockchain_event(payload, wf_id)
        elif m_type == "RECONCILE_STATE":
            return await self._reconcile_state(payload, wf_id)
        elif m_type == "CHECK_DEADLINES":
            return await self._check_deadlines(payload, wf_id)
        else:
            # Fallback based on payload content
            if "event_name" in payload or "event" in payload:
                return await self._process_blockchain_event(payload, wf_id)
            elif "tx_hash" in payload or "transaction_hash" in payload:
                return await self._check_transaction(payload, wf_id)
            else:
                return await self._check_deadlines(payload, wf_id)

    async def _check_transaction(self, payload: dict[str, Any], workflow_id: UUID) -> dict[str, Any]:
        """Check confirmation and receipt status of a submitted transaction."""
        tx_hash = payload.get("tx_hash") or payload.get("transaction_hash")
        if not tx_hash:
            raise ValueError("CHECK_TRANSACTION requires 'tx_hash' in payload")

        submitted_at_raw = payload.get("submitted_at")
        now = datetime.now(timezone.utc)

        try:
            res = self.reader.get_transaction_receipt(tx_hash)
        except Exception as exc:
            logger.warning("RPC error checking transaction %s: %s", tx_hash, exc)
            return {
                "success": False,
                "status": "RPC_ERROR",
                "tx_hash": tx_hash,
                "workflow_id": str(workflow_id),
                "confirmed": False,
                "error": f"RPC connection error: {exc}",
            }

        if not res or not res.get("success"):
            err = res.get("error", "Unknown receipt error") if isinstance(res, dict) else "No response from RPC"
            if isinstance(res, dict) and res.get("status") == "replaced":
                replacement_hash = res.get("replacement_tx_hash")
                return {
                    "success": False,
                    "status": "REPLACED",
                    "tx_hash": tx_hash,
                    "replacement_tx_hash": replacement_hash,
                    "workflow_id": str(workflow_id),
                    "confirmed": False,
                    "error": err,
                    "message": f"Transaction {tx_hash} replaced by {replacement_hash}",
                }

            # Check for delayed confirmation
            is_delayed = False
            if submitted_at_raw:
                try:
                    submitted_at = (
                        datetime.fromisoformat(submitted_at_raw)
                        if isinstance(submitted_at_raw, str)
                        else submitted_at_raw
                    )
                    elapsed = (now - submitted_at).total_seconds()
                    if elapsed > self.confirmation_timeout_seconds:
                        is_delayed = True
                except Exception:
                    pass

            return {
                "success": False,
                "status": "DELAYED" if is_delayed else "PENDING",
                "tx_hash": tx_hash,
                "workflow_id": str(workflow_id),
                "confirmed": False,
                "is_delayed": is_delayed,
                "error": err,
                "message": (
                    f"Transaction {tx_hash} confirmation is delayed ({err})"
                    if is_delayed
                    else f"Transaction {tx_hash} is pending"
                ),
            }

        # Check receipt status
        receipt_status = res.get("status")
        block_number = res.get("blockNumber") or res.get("block_number")

        if receipt_status in ("reverted", "failed", 0, "0x0"):
            return {
                "success": False,
                "status": "REVERTED",
                "tx_hash": tx_hash,
                "workflow_id": str(workflow_id),
                "confirmed": True,
                "block_number": block_number,
                "error": res.get("revertReason") or res.get("error") or "Transaction reverted on-chain",
                "message": f"Transaction {tx_hash} reverted in block {block_number}",
            }
        confirmations = res.get("confirmations", 1)
        if hasattr(self, "required_confirmations") and confirmations < self.required_confirmations:
            return {
                "success": False,
                "status": "AWAITING_CONFIRMATIONS",
                "tx_hash": tx_hash,
                "workflow_id": str(workflow_id),
                "confirmed": False,
                "confirmations": confirmations,
                "required_confirmations": self.required_confirmations,
                "message": f"Transaction {tx_hash} has {confirmations}/{self.required_confirmations} required confirmations",
            }

        return {
            "success": True,
            "status": "CONFIRMED",
            "tx_hash": tx_hash,
            "workflow_id": str(workflow_id),
            "confirmed": True,
            "block_number": block_number,
            "gas_used": res.get("gasUsed"),
            "message": f"Transaction {tx_hash} confirmed in block {block_number}",
        }

    async def _process_blockchain_event(self, payload: dict[str, Any], workflow_id: UUID) -> dict[str, Any]:
        """Process, deduplicate, and synchronize an incoming smart contract event."""
        event_dict = payload.get("event") or payload
        event = BlockchainEvent.model_validate(event_dict)

        # Invariant 4: Avoid processing the same event more than once
        if event.event_key in self._processed_event_keys:
            logger.info("Ignoring duplicate event: %s", event.event_key)
            return {
                "success": True,
                "duplicate": True,
                "processed": False,
                "event_key": event.event_key,
                "event_name": event.event_name,
                "message": f"Duplicate event '{event.event_key}' ignored",
            }

        self._processed_event_keys.add(event.event_key)

        entity_id = (
            event.payload.get("reservationId")
            or event.payload.get("reservation_id")
            or event.payload.get("lockId")
            or event.payload.get("lock_id")
            or event.transaction_hash
        )

        entity_type = "lock" if "lock" in event.event_name.lower() else "reservation"
        entity = self._get_or_create_entity(entity_id, entity_type, workflow_id)

        now = datetime.now(timezone.utc)
        prev_state = entity.state
        new_state = prev_state

        # State mapping based on authoritative smart contract event names
        name = event.event_name
        if name == "CapacityLocked":
            new_state = ReservationLifecycleState.LOCKED
            if "expiresAt" in event.payload:
                exp_ts = int(event.payload["expiresAt"])
                entity.expires_at = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        elif name == "CapacityLockConverted":
            new_state = ReservationLifecycleState.COMMITTED
        elif name == "CapacityLockReleased":
            new_state = ReservationLifecycleState.CANCELLED
        elif name == "ReservationCreated":
            new_state = ReservationLifecycleState.PROPOSED
        elif name == "ReservationCommitted":
            new_state = ReservationLifecycleState.COMMITTED
        elif name == "ReservationFulfilled":
            # Invariant 3: Authoritative fulfillment verification
            fulfilled_ts = event.payload.get("fulfilledAt")
            if fulfilled_ts:
                new_state = ReservationLifecycleState.FULFILLED
                entity.fulfilled_at = datetime.fromtimestamp(int(fulfilled_ts), tz=timezone.utc)
            else:
                logger.warning("ReservationFulfilled event missing fulfilledAt timestamp; state unchanged")
        elif name == "ReservationCancelled":
            new_state = ReservationLifecycleState.CANCELLED
        elif name == "ReservationExpired":
            new_state = ReservationLifecycleState.EXPIRED
        elif name == "ReservationDisputed":
            new_state = ReservationLifecycleState.DISPUTED
            entity.dispute_evidence = event.payload.get("evidenceHash")

        entity.state = new_state
        entity.tx_hash = event.transaction_hash
        entity.block_number = event.block_number

        # Record audit lineage
        transition_record = {
            "from_state": prev_state.value,
            "to_state": new_state.value,
            "event_name": event.event_name,
            "tx_hash": event.transaction_hash,
            "block_number": event.block_number,
            "timestamp": now.isoformat(),
        }
        entity.history.append(transition_record)
        entity.last_checked_at = now

        return {
            "success": True,
            "duplicate": False,
            "processed": True,
            "event_key": event.event_key,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "from_state": prev_state.value,
            "state": new_state.value,
            "tx_hash": event.transaction_hash,
            "block_number": event.block_number,
            "workflow_id": str(workflow_id),
            "history": entity.history,
            "message": f"Event '{event.event_name}' synchronized state: {prev_state.value} -> {new_state.value}",
        }

    async def _reconcile_state(self, payload: dict[str, Any], workflow_id: UUID) -> dict[str, Any]:
        """Reconcile local state with authoritative on-chain contract storage."""
        lock_id = payload.get("lock_id")
        reservation_id = payload.get("reservation_id")

        if lock_id:
            res = self.reader.get_lock(str(lock_id))
            if not res or not res.get("success"):
                raise RuntimeError(f"Failed to reconcile lock '{lock_id}': {res.get('error')}")

            entity = self._get_or_create_entity(str(lock_id), "lock", workflow_id)
            onchain_status = res.get("status")
            now = datetime.now(timezone.utc)

            # Map LockStatus: 1: Active, 2: Converted, 3: Released
            if onchain_status in (1, "Active", "active"):
                entity.state = ReservationLifecycleState.LOCKED
            elif onchain_status in (2, "Converted", "converted"):
                entity.state = ReservationLifecycleState.COMMITTED
            elif onchain_status in (3, "Released", "released"):
                entity.state = ReservationLifecycleState.CANCELLED

            if "expiresAt" in res:
                entity.expires_at = datetime.fromtimestamp(int(res["expiresAt"]), tz=timezone.utc)

            entity.last_checked_at = now
            return {
                "success": True,
                "entity_id": str(lock_id),
                "state": entity.state.value,
                "expires_at": entity.expires_at.isoformat() if entity.expires_at else None,
                "onchain_status": onchain_status,
            }

        elif reservation_id:
            res = self.reader.get_reservation(str(reservation_id))
            if not res or not res.get("success"):
                raise RuntimeError(f"Failed to reconcile reservation '{reservation_id}': {res.get('error')}")

            entity = self._get_or_create_entity(str(reservation_id), "reservation", workflow_id)
            onchain_status = res.get("status")
            now = datetime.now(timezone.utc)

            # Map Status: 0: Proposed, 1: Committed, 2: Fulfilled, 3: Cancelled, 4: Expired, 5: Disputed
            if onchain_status in (0, "Proposed", "proposed"):
                entity.state = ReservationLifecycleState.PROPOSED
            elif onchain_status in (1, "Committed", "committed"):
                entity.state = ReservationLifecycleState.COMMITTED
            elif onchain_status in (2, "Fulfilled", "fulfilled"):
                # Invariant 3: Fulfill strictly if proof or fulfilledAt is present
                fulfilled_at_raw = res.get("fulfilledAt")
                if fulfilled_at_raw and int(fulfilled_at_raw) > 0:
                    entity.state = ReservationLifecycleState.FULFILLED
                    entity.fulfilled_at = datetime.fromtimestamp(int(fulfilled_at_raw), tz=timezone.utc)
            elif onchain_status in (3, "Cancelled", "cancelled"):
                entity.state = ReservationLifecycleState.CANCELLED
            elif onchain_status in (4, "Expired", "expired"):
                entity.state = ReservationLifecycleState.EXPIRED
            elif onchain_status in (5, "Disputed", "disputed"):
                entity.state = ReservationLifecycleState.DISPUTED

            entity.last_checked_at = now
            return {
                "success": True,
                "entity_id": str(reservation_id),
                "state": entity.state.value,
                "onchain_status": onchain_status,
            }
        else:
            raise ValueError("RECONCILE_STATE requires 'lock_id' or 'reservation_id'")

    async def _check_deadlines(self, payload: dict[str, Any], workflow_id: UUID) -> dict[str, Any]:
        """Detect expired capacity locks and approaching commitment deadlines."""
        now = datetime.now(timezone.utc)
        expired_locks: list[str] = []
        approaching_deadlines: list[dict[str, Any]] = []

        # Target specific entity if provided, or evaluate all tracked entities
        target_id = payload.get("lock_id") or payload.get("reservation_id") or payload.get("entity_id")
        targets = [self._entities[target_id]] if target_id and target_id in self._entities else list(self._entities.values())

        # If payload provides custom expiration timestamps directly:
        if payload.get("expires_at"):
            raw_exp = payload["expires_at"]
            exp_time = datetime.fromisoformat(raw_exp) if isinstance(raw_exp, str) else raw_exp
            entity_id = target_id or "custom_target"
            ent = self._get_or_create_entity(entity_id, "lock", workflow_id)
            ent.expires_at = exp_time
            targets = [ent]

        for ent in targets:
            if not ent.expires_at:
                continue

            # Only check active locks or reservations
            if ent.state not in (ReservationLifecycleState.LOCKED, ReservationLifecycleState.PROPOSED, ReservationLifecycleState.COMMITTED):
                continue

            remaining_seconds = (ent.expires_at - now).total_seconds()

            if remaining_seconds <= 0:
                # Lock has elapsed
                ent.state = ReservationLifecycleState.EXPIRED
                expired_locks.append(ent.entity_id)
                ent.history.append({
                    "action": "LOCK_EXPIRED",
                    "timestamp": now.isoformat(),
                    "expires_at": ent.expires_at.isoformat(),
                })
            elif remaining_seconds <= self.deadline_warning_seconds:
                # Approaching deadline
                approaching_deadlines.append({
                    "entity_id": ent.entity_id,
                    "remaining_seconds": round(remaining_seconds, 1),
                    "expires_at": ent.expires_at.isoformat(),
                })

        return {
            "success": True,
            "expired_locks": expired_locks,
            "approaching_deadlines": approaching_deadlines,
            "evaluated_count": len(targets),
            "timestamp": now.isoformat(),
        }

    def _get_or_create_entity(self, entity_id: str, entity_type: str, workflow_id: Optional[UUID]) -> MonitoredEntity:
        """Retrieve or initialize monitored entity snapshot."""
        if entity_id not in self._entities:
            entity = MonitoredEntity(
                entity_id=entity_id,
                entity_type=entity_type,
                workflow_id=workflow_id,
            )
            self._entities[entity_id] = entity
            if workflow_id:
                if workflow_id not in self._workflow_mappings:
                    self._workflow_mappings[workflow_id] = []
                self._workflow_mappings[workflow_id].append(entity_id)
        return self._entities[entity_id]

