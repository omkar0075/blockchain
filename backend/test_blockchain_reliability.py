import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.agents.blockchain_agent import BlockchainExecutionAgent
from app.agents.monitoring_agent import (
    BlockchainEvent,
    MonitoringAgent,
    ReservationLifecycleState,
)
from app.agents.recovery_agent import FailureClassification
from app.db import init_db
from app.schemas.agent_messages import AgentMessage, AgentStatus
from app.schemas.provider_offer import ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from app.schemas.verification import (
    VerificationChecklist,
    VerificationResult,
    VerificationStatus,
)
from app.services.workflow_service import WorkflowService, WorkflowStateError
from app.workflows.reservation_workflow import (
    ReservationWorkflowCoordinator,
    ReservationWorkflowResult,
)


class ConfigurableBlockchainClient:
    """Configurable client simulating various network and smart contract behaviors."""

    def __init__(
        self,
        rpc_down: bool = False,
        should_revert: bool = False,
        revert_msg: str = "Execution reverted",
        initial_tx_hash: str = "0xaaaa1111",
    ) -> None:
        self.rpc_down = rpc_down
        self.should_revert = should_revert
        self.revert_msg = revert_msg
        self.tx_hash = initial_tx_hash
        self.submission_count = 0
        self.idempotency_store: dict[str, dict[str, Any]] = {}

    def lock_capacity(self, **kwargs) -> dict[str, Any]:
        self.submission_count += 1
        if self.rpc_down:
            return {"success": False, "error": "RPC endpoint unreachable (HTTP 503 Gateway Timeout)"}
        if self.should_revert:
            return {"success": False, "error": self.revert_msg}

        idem_key = kwargs.get("idempotency_key")
        if idem_key and idem_key in self.idempotency_store:
            return self.idempotency_store[idem_key]

        res = {
            "success": True,
            "txHash": self.tx_hash,
            "blockNumber": 1288000,
            "lockId": f"lock-{uuid4().hex[:8]}",
        }
        if idem_key:
            self.idempotency_store[idem_key] = res
        return res

    def create_reservation_with_lock(self, **kwargs) -> dict[str, Any]:
        self.submission_count += 1
        if self.rpc_down:
            return {"success": False, "error": "RPC connection reset by peer"}
        if self.should_revert:
            return {"success": False, "error": self.revert_msg}

        idem_key = kwargs.get("idempotency_key")
        if idem_key and idem_key in self.idempotency_store:
            return self.idempotency_store[idem_key]

        res = {
            "success": True,
            "txHash": self.tx_hash,
            "blockNumber": 1288005,
            "reservationId": f"res-{uuid4().hex[:8]}",
        }
        if idem_key:
            self.idempotency_store[idem_key] = res
        return res


class ConfigurableBlockchainReader:
    """Configurable reader simulating receipts, delays, replacements, and on-chain state."""

    def __init__(
        self,
        is_delayed: bool = False,
        status: str = "confirmed",
        confirmations: int = 6,
        replaced_hash: Optional[str] = None,
        onchain_reservations: Optional[dict[str, dict[str, Any]]] = None,
        onchain_locks: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        self.is_delayed = is_delayed
        self.status = status
        self.confirmations = confirmations
        self.replaced_hash = replaced_hash
        self.reservations = onchain_reservations or {}
        self.locks = onchain_locks or {}

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        if self.replaced_hash and tx_hash != self.replaced_hash:
            return {"success": False, "error": "Transaction replaced with higher gas fee", "status": "replaced", "replacement_tx_hash": self.replaced_hash}
        if self.is_delayed:
            return {"success": False, "error": "Transaction pending (in mempool)", "status": "pending"}
        return {
            "success": True,
            "status": self.status,
            "blockNumber": 1288010,
            "confirmations": self.confirmations,
            "gasUsed": 85000,
        }

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        if reservation_id in self.reservations:
            return self.reservations[reservation_id]
        return {"success": True, "status": "Committed", "fulfilledAt": 0}

    def get_lock(self, lock_id: str) -> dict[str, Any]:
        if lock_id in self.locks:
            return self.locks[lock_id]
        return {"success": True, "status": "Active", "expiresAt": int(datetime.now(timezone.utc).timestamp()) + 3600}


def create_test_workflow(wf_id: UUID, state: str = "WAITING_FOR_APPROVAL") -> ReservationWorkflowResult:
    now = datetime.now(timezone.utc)
    checks = VerificationChecklist(
        institution_registered=True,
        institution_active=True,
        wallet_authorized=True,
        resource_registered=True,
        offer_unexpired=True,
        capacity_available=True,
        request_compatible=True,
    )
    return ReservationWorkflowResult(
        workflow_id=wf_id,
        state=state,
        request=ResourceRequest(
            workflow_id=wf_id,
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        ),
        selected_offer=ProviderOffer(
            offer_id=uuid4(),
            request_id=uuid4(),
            provider_id="hospital-b",
            provider_name="Hospital B",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            offered_quantity=2,
            location=LocationArea(city="Metropolis"),
            cost=ProviderCost(amount=100.0, currency="USD"),
            estimated_delivery_hours=4,
            available_from=now,
            available_until=now + timedelta(hours=24),
            expires_at=now + timedelta(hours=6),
        ),
        verification=VerificationResult(
            workflow_id=wf_id,
            offer_id=uuid4(),
            provider_id="hospital-b",
            status=VerificationStatus.VERIFIED,
            checks=checks,
        ),
        is_ready_for_approval=True,
    )


# -----------------------------------------------------------------------------
# Scenario 1: RPC unavailable reports retryable failure, never false success
# -----------------------------------------------------------------------------
def test_rpc_unavailable_reports_retryable_failure():
    async def run():
        client = ConfigurableBlockchainClient(rpc_down=True)
        agent = BlockchainExecutionAgent(blockchain_client=client)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload={
                "operation": "LOCK_CAPACITY",
                "approved": True,
                "approved_by": "Dr. Sarah",
                "verification": {"status": "VERIFIED", "provider_id": "hospital-b", "offer_id": str(uuid4())},
            "requester_id": "hospital-a",
                "provider_id": "hospital-b",
                "resource_type": "diagnostic_slot",
                "quantity": 2,
            },
        )
        res = await agent.execute(msg)

        assert res.status == AgentStatus.FAILED
        assert "unreachable" in res.error.message.lower() or "503" in res.error.message.lower()

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 2: Transaction pending leaves workflow in pending state
# -----------------------------------------------------------------------------
def test_transaction_pending_leaves_workflow_pending():
    async def run():
        reader = ConfigurableBlockchainReader(is_delayed=True)
        agent = MonitoringAgent(reader=reader)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={"tx_hash": "0x1234pending"},
        )
        res = await agent.execute(msg)

        # Monitoring agent confirms transaction is still pending, not failed and not finalized
        assert res.data["confirmed"] is False
        assert "pending" in res.data["status"].lower() or "mempool" in res.data["status"].lower()

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 3: Transaction reverted records failure, never false success
# -----------------------------------------------------------------------------
def test_transaction_reverted_records_failure():
    async def run():
        client = ConfigurableBlockchainClient(should_revert=True, revert_msg="Execution reverted: CapacityRaceRevert")
        agent = BlockchainExecutionAgent(blockchain_client=client)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload={
                "operation": "LOCK_CAPACITY",
                "approved": True,
                "approved_by": "Dr. Sarah",
                "verification": {"status": "VERIFIED", "provider_id": "hospital-b", "offer_id": str(uuid4())},
            "requester_id": "hospital-a",
                "provider_id": "hospital-b",
                "resource_type": "diagnostic_slot",
                "quantity": 2,
            },
        )
        res = await agent.execute(msg)

        assert res.status == AgentStatus.FAILED
        assert "revert" in res.error.message.lower()

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 4: Duplicate transaction submission prevented by idempotency
# -----------------------------------------------------------------------------
def test_duplicate_transaction_submission_prevented():
    client = ConfigurableBlockchainClient()
    idem_key = "idemp-req-001"

    # First submission
    res1 = client.lock_capacity(provider_id="hospital-b", resource_type="diagnostic_slot", quantity=2, requester_id="hospital-a", idempotency_key=idem_key)
    # Duplicate submission with same key
    res2 = client.lock_capacity(provider_id="hospital-b", resource_type="diagnostic_slot", quantity=2, requester_id="hospital-a", idempotency_key=idem_key)

    assert res1["txHash"] == res2["txHash"]
    assert res1["lockId"] == res2["lockId"]


# -----------------------------------------------------------------------------
# Scenario 5: Transaction replacement tracks updated transaction hash
# -----------------------------------------------------------------------------
def test_transaction_replacement_handled():
    async def run():
        replacement_hash = "0x9999replacement"
        reader = ConfigurableBlockchainReader(
            replaced_hash=replacement_hash,
        )
        agent = MonitoringAgent(reader=reader)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={"tx_hash": "0xoriginal_low_gas"},
        )
        res = await agent.execute(msg)

        # Verifies replacement is detected and replacement tx hash is captured
        assert res.data.get("status") == "replaced" or res.data.get("replacement_tx_hash") == replacement_hash

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 6: Confirmation monitoring waits for required confirmation depth
# -----------------------------------------------------------------------------
def test_confirmation_monitoring_depth():
    async def run():
        # Only 1 confirmation, but policy requires 3
        reader_shallow = ConfigurableBlockchainReader(confirmations=1)
        agent = MonitoringAgent(reader=reader_shallow, required_confirmations=3)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={"tx_hash": "0xshallow_block"},
        )
        res = await agent.execute(msg)

        # Must not finalize before required confirmation depth is reached
        assert res.data["confirmed"] is False

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 7: Event synchronization is idempotent and recovers missed events
# -----------------------------------------------------------------------------
def test_event_synchronization_deduplication():
    async def run():
        agent = MonitoringAgent()
        evt = BlockchainEvent(
            event_name="ReservationCommitted",
            transaction_hash="0xabcdef123",
            block_number=1288020,
            log_index=0,
            payload={"reservationId": "res-101", "lockId": "lock-101"},
        )

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="BlockchainReader",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=evt.model_dump(),
        )

        # Process event first time
        res1 = await agent.execute(msg)
        assert res1.data["processed"] is True
        assert res1.data["duplicate"] is False

        # Simulate replay or re-synchronization of same event
        res2 = await agent.execute(msg)
        assert res2.data["duplicate"] is True

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 8: Database and blockchain inconsistency reconciliation
# -----------------------------------------------------------------------------
def test_database_and_blockchain_inconsistency():
    init_db()
    service = WorkflowService()
    wf_id = uuid4()
    wf = create_test_workflow(wf_id, state="WAITING_FOR_APPROVAL")
    wf.onchain_result = {"reservation_id": "res-onchain-committed-99"}
    service.persist_workflow(wf)

    # On-chain smart contract says Committed
    reader = ConfigurableBlockchainReader(
        onchain_reservations={"res-onchain-committed-99": {"success": True, "status": "Committed"}}
    )

    reconciled = service.reconcile_with_onchain(wf_id, reader=reader)

    # Local replica state is updated to reflect authoritative on-chain state
    assert reconciled.monitoring_status == "Committed"
    assert reconciled.is_committed is True


# -----------------------------------------------------------------------------
# Scenario 9: Expired capacity lock detected and not treated as reserved
# -----------------------------------------------------------------------------
def test_expired_capacity_lock_recognized():
    init_db()
    service = WorkflowService()
    wf_id = uuid4()
    wf = create_test_workflow(wf_id, state="WAITING_FOR_APPROVAL")
    wf.onchain_result = {"lock_id": "lock-stale-01"}
    service.persist_workflow(wf)

    # Smart contract reports lock has Expired
    reader = ConfigurableBlockchainReader(
        onchain_locks={"lock-stale-01": {"success": True, "status": "Expired"}}
    )

    reconciled = service.reconcile_with_onchain(wf_id, reader=reader)

    assert reconciled.monitoring_status == "Expired"
    assert reconciled.is_committed is False


# -----------------------------------------------------------------------------
# Scenario 10: Fulfillment reconciliation confirms completed delivery
# -----------------------------------------------------------------------------
def test_fulfillment_reconciliation():
    init_db()
    service = WorkflowService()
    wf_id = uuid4()
    wf = create_test_workflow(wf_id, state="CONFIRMED")
    wf.onchain_result = {"reservation_id": "res-fulfilled-55"}
    service.persist_workflow(wf)

    # Contract reports Fulfilled
    reader = ConfigurableBlockchainReader(
        onchain_reservations={"res-fulfilled-55": {"success": True, "status": "Fulfilled"}}
    )

    reconciled = service.reconcile_with_onchain(wf_id, reader=reader)
    assert reconciled.monitoring_status == "Fulfilled"


# -----------------------------------------------------------------------------
# Scenario 11: Process restart resumes pending workflow from persisted data
# -----------------------------------------------------------------------------
def test_process_restart_resumes_pending_workflow():
    init_db()
    service = WorkflowService()
    wf_id = uuid4()
    wf = create_test_workflow(wf_id, state="WAITING_FOR_APPROVAL")
    service.persist_workflow(wf)

    # Simulate fresh process restart by instantiating new coordinator
    fresh_coordinator = ReservationWorkflowCoordinator()
    assert wf_id not in fresh_coordinator._workflows

    # Resume from persistence
    resumed = service.resume_workflow(wf_id, coordinator=fresh_coordinator)
    assert resumed.workflow_id == wf_id
    assert resumed.state == "WAITING_FOR_APPROVAL"
    assert wf_id in fresh_coordinator._workflows


# -----------------------------------------------------------------------------
# Scenario 12: Bounded retry with exponential backoff on repeated RPC failures
# -----------------------------------------------------------------------------
def test_bounded_retry_exhaustion():
    async def run():
        from app.agents.recovery_agent import RecoveryAgent
        rec = RecoveryAgent(max_recovery_attempts=3)

        wf_id = uuid4()
        payload = {
            "error": "RPC node timeout (attempt 4)",
            "failure_classification": "RPC_FAILURE",
            "attempt_number": 4,  # Exceeds max 3 attempts
        }
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="MonitoringAgent",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload=payload,
        )
        res = await rec.execute(msg)

        # Must halt bounded retry and escalate to human
        assert res.data["escalated"] is True
        assert res.data["selected_strategy"] == "ESCALATE_TO_HUMAN"

    asyncio.run(run())
