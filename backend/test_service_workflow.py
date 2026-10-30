import asyncio
import pytest
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from app.db import (
    delete_workflow_record,
    get_workflow_record,
    get_workflow_transitions,
    init_db,
)
from app.schemas.resource_request import LocationArea, ResourceRequest, ResourceType, UrgencyLevel
from app.schemas.verification import VerificationStatus
from app.services.workflow_service import (
    WorkflowSecurityError,
    WorkflowService,
    WorkflowStateError,
    WorkflowStorageError,
)
from app.workflows.reservation_workflow import (
    ReservationWorkflowCoordinator,
    ReservationWorkflowResult,
)


@pytest.fixture(autouse=True)
def ensure_db():
    init_db()


@pytest.fixture
def service():
    return WorkflowService()


def create_verified_workflow() -> ReservationWorkflowResult:
    """Helper to generate a real, fully-validated workflow paused at WAITING_FOR_APPROVAL."""
    async def _start():
        coordinator = ReservationWorkflowCoordinator()
        now = datetime.now(timezone.utc)
        req = ResourceRequest(
            request_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=3,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )
        wf_id = uuid4()
        res = await coordinator.start(req, workflow_id=wf_id)
        assert res.state == "WAITING_FOR_APPROVAL"
        return res

    return asyncio.run(_start())


@pytest.fixture
def sample_workflow():
    wf = create_verified_workflow()
    yield wf
    delete_workflow_record(str(wf.workflow_id))


# -----------------------------------------------------------------------------
# Test 1: Persists and reloads a workflow
# -----------------------------------------------------------------------------
def test_persists_and_reloads_workflow(service, sample_workflow):
    service.persist_workflow(sample_workflow)

    loaded = service.load_workflow(sample_workflow.workflow_id)
    assert loaded is not None
    assert loaded.workflow_id == sample_workflow.workflow_id
    assert loaded.state == "WAITING_FOR_APPROVAL"
    assert loaded.request.requester_institution_id == "hospital-a"
    assert loaded.request.quantity == 3
    assert loaded.selected_offer.provider_id == "lab-c"
    assert loaded.verification.status == VerificationStatus.VERIFIED
    assert loaded.is_ready_for_approval is True
    assert loaded.is_committed is False


# -----------------------------------------------------------------------------
# Test 2: Preserves transition history
# -----------------------------------------------------------------------------
def test_preserves_transition_history(service, sample_workflow):
    service.persist_workflow(sample_workflow)

    service.record_transition(
        workflow_id=sample_workflow.workflow_id,
        from_state="WAITING_FOR_APPROVAL",
        to_state="APPROVED",
        agent="InstitutionalOfficer",
        details="Authorized by Dr. Smith",
    )
    service.record_transition(
        workflow_id=sample_workflow.workflow_id,
        from_state="APPROVED",
        to_state="COMMITTING_ONCHAIN",
        agent="BlockchainExecutionAgent",
        details="Executing capacity lock on Coral Testnet",
    )

    history = service.get_transition_history(sample_workflow.workflow_id)
    assert len(history) >= 4  # Initial audit trail + newly recorded transitions
    states = [h["to_state"] for h in history]
    assert "DISCOVERING_PROVIDERS" in states
    assert "WAITING_FOR_APPROVAL" in states
    assert "APPROVED" in states
    assert "COMMITTING_ONCHAIN" in states


class MockBlockchainClient:
    def __init__(self) -> None:
        self.lock_response = {
            "success": True,
            "txHash": "0x4a9b6c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b",
            "blockNumber": 1286000,
            "lockId": "0x1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        }
        self.reservation_response = {
            "success": True,
            "txHash": "0x9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e",
            "blockNumber": 1286005,
            "reservationId": "0x2222333344445555666677778888999900001111bbbbccccddddeeeeffffaaaa",
        }

    def lock_capacity(self, **kwargs) -> dict[str, Any]:
        return self.lock_response

    def create_reservation_with_lock(self, **kwargs) -> dict[str, Any]:
        return self.reservation_response


class MockBlockchainReader:
    def get_transaction_receipt(self, tx_hash: str):
        return {
            "success": True,
            "status": "confirmed",
            "blockNumber": 1286005,
        }


# -----------------------------------------------------------------------------
# Test 3: Resumes an interrupted workflow
# -----------------------------------------------------------------------------
def test_resumes_interrupted_workflow(service, sample_workflow):
    async def run():
        service.persist_workflow(sample_workflow)

        # Simulate fresh backend coordinator following restart
        new_coordinator = ReservationWorkflowCoordinator(
            blockchain_client=MockBlockchainClient(),
            blockchain_reader=MockBlockchainReader(),
        )
        assert sample_workflow.workflow_id not in new_coordinator._workflows

        # Resume workflow into coordinator
        resumed = service.resume_workflow(sample_workflow.workflow_id, coordinator=new_coordinator)
        assert resumed.workflow_id == sample_workflow.workflow_id
        assert new_coordinator._workflows[sample_workflow.workflow_id].state == "WAITING_FOR_APPROVAL"

        # Now institutional officer approves and commits the resumed workflow
        commit_res = await new_coordinator.authorize_and_commit(
            workflow_id=sample_workflow.workflow_id,
            approved_by="Dr. Alice Vance (Chief Medical Officer)",
        )
        assert commit_res.state == "CONFIRMED"
        assert commit_res.is_committed is True
        assert commit_res.approved_by == "Dr. Alice Vance (Chief Medical Officer)"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 4: Prevents duplicate execution after restart
# -----------------------------------------------------------------------------
def test_prevents_duplicate_execution_after_restart(service, sample_workflow):
    sample_workflow.is_committed = True
    sample_workflow.state = "CONFIRMED"
    sample_workflow.onchain_result = {
        "status": "CONFIRMED",
        "tx_hash": "0xabc1234567890abcdef1234567890abcdef1234567890abcdef1234567890abc",
        "block_number": 9942,
    }
    service.persist_workflow(sample_workflow)

    # Attempting to resume an already committed workflow must be prevented
    with pytest.raises(WorkflowStateError, match="Duplicate on-chain execution is strictly prevented"):
        service.resume_workflow(sample_workflow.workflow_id)


# -----------------------------------------------------------------------------
# Test 5: Restores pending human approvals
# -----------------------------------------------------------------------------
def test_restores_pending_human_approvals(service, sample_workflow):
    service.persist_workflow(sample_workflow)

    interrupted = service.list_interrupted_workflows()
    matching = [w for w in interrupted if w.workflow_id == sample_workflow.workflow_id]
    assert len(matching) == 1
    pending = matching[0]
    assert pending.state == "WAITING_FOR_APPROVAL"
    assert pending.is_ready_for_approval is True
    assert pending.approved_by is None
    assert pending.selected_offer.offered_quantity == 3


# -----------------------------------------------------------------------------
# Test 6: Reconciles transaction status against authoritative on-chain state
# -----------------------------------------------------------------------------
def test_reconciles_transaction_status(service, sample_workflow):
    sample_workflow.state = "COMMITTING_ONCHAIN"
    sample_workflow.onchain_result = {
        "tx_hash": "0x7890abcdef1234567890abcdef1234567890abcdef1234567890abcdef123456",
        "status": "PENDING",
    }
    service.persist_workflow(sample_workflow)

    mock_reader = MagicMock()
    mock_reader.get_transaction_receipt.return_value = {
        "status": 1,
        "blockNumber": 10500,
        "transactionHash": "0x7890abcdef1234567890abcdef1234567890abcdef1234567890abcdef123456",
    }

    reconciled = service.reconcile_with_onchain(sample_workflow.workflow_id, reader=mock_reader)
    assert reconciled.state == "CONFIRMED"
    assert reconciled.is_committed is True
    assert reconciled.monitoring_status == "CONFIRMED"
    assert reconciled.onchain_result["block_number"] == 10500

    # Test revert reconciliation
    sample_workflow.state = "COMMITTING_ONCHAIN"
    sample_workflow.is_committed = False
    service.persist_workflow(sample_workflow)

    revert_reader = MagicMock()
    revert_reader.get_transaction_receipt.return_value = {
        "status": 0,
        "blockNumber": 10501,
        "transactionHash": "0x7890abcdef1234567890abcdef1234567890abcdef1234567890abcdef123456",
    }

    reconciled_rev = service.reconcile_with_onchain(sample_workflow.workflow_id, reader=revert_reader)
    assert reconciled_rev.state == "REVERTED"
    assert reconciled_rev.is_committed is False
    assert reconciled_rev.monitoring_status == "REVERTED"


# -----------------------------------------------------------------------------
# Test 7: Handles database failures
# -----------------------------------------------------------------------------
def test_handles_database_failures(sample_workflow, monkeypatch):
    service = WorkflowService()

    def bad_connection(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated disk I/O failure or corrupted database")

    monkeypatch.setattr("app.services.workflow_service.save_workflow_record", bad_connection)

    with pytest.raises(WorkflowStorageError, match="Database error persisting workflow"):
        service.persist_workflow(sample_workflow)


# -----------------------------------------------------------------------------
# Test 8: Rejects secrets and patient PII
# -----------------------------------------------------------------------------
def test_rejects_secrets_and_pii(service, sample_workflow):
    # Prohibited private key in audit trail details
    sample_workflow.audit_trail.append({
        "from_state": "APPROVED",
        "to_state": "SIGNING",
        "agent": "Attacker",
        "details": "Injecting private_key 0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    })

    with pytest.raises(WorkflowSecurityError, match="forbidden pattern 'private_key'"):
        service.persist_workflow(sample_workflow)

    # Prohibited patient identifying information (PII)
    sample_workflow.audit_trail.pop()
    sample_workflow.audit_trail.append({
        "from_state": "APPROVED",
        "to_state": "PATIENT_DATA",
        "agent": "Attacker",
        "details": "Patient SSN is 000-11-2222",
    })

    with pytest.raises(WorkflowSecurityError, match="forbidden pattern 'ssn'"):
        service.persist_workflow(sample_workflow)
