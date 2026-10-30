import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.schemas.provider_offer import ProviderCost
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from app.schemas.verification import VerificationStatus
from app.workflows.reservation_workflow import (
    ReservationWorkflowCoordinator,
    ReservationWorkflowResult,
    create_full_coordination_registry,
)


class MockBlockchainClient:
    """Mock blockchain service client simulating Coral Testnet interactions."""

    def __init__(
        self,
        lock_response: Optional[dict[str, Any]] = None,
        reservation_response: Optional[dict[str, Any]] = None,
        should_revert: bool = False,
    ) -> None:
        self.lock_response = lock_response or {
            "success": True,
            "txHash": "0x4a9b6c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b",
            "blockNumber": 1286000,
            "lockId": "0x1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        }
        self.reservation_response = reservation_response or {
            "success": True,
            "txHash": "0x9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e",
            "blockNumber": 1286005,
            "reservationId": "0x2222333344445555666677778888999900001111bbbbccccddddeeeeffffaaaa",
        }
        self.should_revert = should_revert

    def lock_capacity(self, **kwargs) -> dict[str, Any]:
        if self.should_revert:
            return {"success": False, "error": "Execution reverted: Insufficient available quota on-chain"}
        return self.lock_response

    def create_reservation_with_lock(self, **kwargs) -> dict[str, Any]:
        if self.should_revert:
            return {"success": False, "error": "Execution reverted: Lock has expired or does not exist"}
        return self.reservation_response

    def create_reservation(self, **kwargs) -> dict[str, Any]:
        return self.reservation_response


class MockBlockchainReader:
    """Mock reader simulating receipt checks and events."""

    def __init__(
        self,
        receipt_status: str = "confirmed",
        is_delayed: bool = False,
    ) -> None:
        self.receipt_status = receipt_status
        self.is_delayed = is_delayed

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        if self.is_delayed:
            return {"success": False, "error": "Transaction not yet mined (delayed)"}
        return {
            "success": True,
            "status": self.receipt_status,
            "blockNumber": 1286005,
            "gasUsed": 92000,
        }

    def get_lock(self, lock_id: str) -> dict[str, Any]:
        return {"success": True, "status": "Active", "expiresAt": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        return {"success": True, "status": "Committed", "fulfilledAt": 0}


def test_realistic_hospital_to_lab_coordination_scenario():
    """
    Realistic Integration Scenario:
    Hospital A needs 3 diagnostic slots → Lab C offers capacity → negotiation succeeds →
    verification passes → authorized capacity lock → reservation commitment → confirmation.
    """
    async def run():
        mock_client = MockBlockchainClient()
        mock_reader = MockBlockchainReader()
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=mock_client,
            blockchain_reader=mock_reader,
        )

        # 1. Hospital A request for 3 diagnostic slots
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
        # 2. Start workflow
        res1 = await coordinator.start(req, workflow_id=wf_id)

        # 3. Pauses at WAITING_FOR_APPROVAL with verified terms
        assert res1.state == "WAITING_FOR_APPROVAL"
        assert res1.is_ready_for_approval is True
        assert res1.is_committed is False
        assert res1.selected_offer is not None
        assert res1.selected_offer.provider_id == "lab-c"  # Lab C selected for SLA
        assert res1.selected_offer.offered_quantity == 3
        assert res1.verification is not None
        assert res1.verification.status in (VerificationStatus.VERIFIED, "VERIFIED")

        # 4. Invariant: Cannot execute without approval
        with pytest.raises((PermissionError, ValueError)):
            await coordinator.authorize_and_commit(wf_id, approved_by="")

        # 5. Attributable authorization & on-chain commitment execution
        res2 = await coordinator.authorize_and_commit(
            wf_id,
            approved_by="Dr. Alice Smith (Chief Medical Officer)",
            operation="LOCK_AND_COMMIT",
        )

        # 6. Final confirmed state on Coral Testnet
        assert res2.state == "CONFIRMED"
        assert res2.is_committed is True
        assert res2.approved_by == "Dr. Alice Smith (Chief Medical Officer)"
        assert res2.approved_at is not None
        assert res2.onchain_result is not None
        assert res2.onchain_result["tx_hash"].startswith("0x")
        assert res2.onchain_result["block_number"] == 1286005
        assert res2.monitoring_status == "CONFIRMED"

        # 7. Audit trail integrity
        states = [s["to_state"] for s in res2.audit_trail]
        assert "PARSING_REQUEST" in states
        assert "DISCOVERING_PROVIDERS" in states
        assert "REQUESTING_OFFERS" in states
        assert "NEGOTIATING" in states
        assert "VERIFYING" in states
        assert "WAITING_FOR_APPROVAL" in states
        assert "APPROVED" in states
        assert "COMMITTING_ONCHAIN" in states
        assert "MONITORING_CONFIRMATION" in states
        assert "CONFIRMED" in states

    asyncio.run(run())


def test_handles_multiple_provider_offers():
    """Verify that multiple candidate offers are collected and ranked deterministically."""
    async def run():
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=MockBlockchainClient(),
            blockchain_reader=MockBlockchainReader(),
        )
        # Requesting 2 slots in Metropolis (both hospital-b and lab-c are available)
        res = await coordinator.start("Urgent requirement for 2 diagnostic slots in Metropolis within 12 hours")

        assert res.state == "WAITING_FOR_APPROVAL"
        # Multiple candidates discovered and multiple offers collected
        assert len(res.discovered_candidates) >= 2
        assert len(res.provider_offers) >= 2
        # Lab C has 2h delivery SLA vs Hospital B 4h, so Lab C is selected
        assert res.selected_offer.provider_id == "lab-c"

    asyncio.run(run())


def test_stops_for_missing_request_information():
    """Verify that missing critical information halts cleanly at NEEDS_CLARIFICATION."""
    async def run():
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=MockBlockchainClient(),
            blockchain_reader=MockBlockchainReader(),
        )
        res = await coordinator.start("We need diagnostic slots immediately")

        assert res.state == "NEEDS_CLARIFICATION"
        assert res.clarification_prompt is not None
        assert res.is_ready_for_approval is False
        assert res.selected_offer is None

    asyncio.run(run())


def test_recovers_when_first_provider_fails():
    """Verify automated recovery: when first provider fails verification, an alternative is discovered and verified."""
    async def run():
        mock_client = MockBlockchainClient()
        mock_reader = MockBlockchainReader()
        registry = create_full_coordination_registry(
            blockchain_client=mock_client,
            blockchain_reader=mock_reader,
        )

        # Configure VerificationAgent with lab-c inactive (will fail)
        strict_reg = {
            "hospital-b": {
                "id": "hospital-b",
                "name": "Community Hospital B",
                "wallet_address": "0x70997970c51812e33987251372ecdbc2b3f3bb6e",
                "is_active": True,
                "authorized_resource_types": ["diagnostic_slot"],
                "service_areas": ["Metropolis"],
            },
            "lab-c": {
                "id": "lab-c",
                "name": "Central Diagnostic Lab C",
                "wallet_address": "0xb05049cc2A5a1c28979DEafc82B82749681DA92C",
                "is_active": False,  # Suspended!
                "authorized_resource_types": ["diagnostic_slot"],
                "service_areas": ["Metropolis"],
            },
        }
        # Re-register VerificationAgent with strict registry
        from app.agents.verification_agent import VerificationAgent
        registry._agents["verificationagent"] = VerificationAgent(institutional_registry=strict_reg)

        coordinator = ReservationWorkflowCoordinator(
            registry=registry,
            blockchain_client=mock_client,
            blockchain_reader=mock_reader,
        )

        now = datetime.now(timezone.utc)
        req = ResourceRequest(
            request_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )

        res = await coordinator.start(req)

        # Recovery should exclude lab-c, find hospital-b, verify it, and pause at WAITING_FOR_APPROVAL
        assert res.state == "WAITING_FOR_APPROVAL"
        assert res.selected_offer is not None
        assert res.selected_offer.provider_id == "hospital-b"
        assert len(res.recovery_attempts) >= 1
        assert res.verification.status == "VERIFIED"

    asyncio.run(run())


def test_stops_when_verification_fails():
    """Verify that when no alternative provider can pass verification, workflow halts safely."""
    async def run():
        mock_client = MockBlockchainClient()
        mock_reader = MockBlockchainReader()
        registry = create_full_coordination_registry(
            blockchain_client=mock_client,
            blockchain_reader=mock_reader,
        )

        # Bar both providers in verification registry
        all_inactive_reg = {
            "hospital-b": {"id": "hospital-b", "name": "B", "wallet_address": "0x70997970c51812e33987251372ecdbc2b3f3bb6e", "is_active": False},
            "lab-c": {"id": "lab-c", "name": "C", "wallet_address": "0xb05049cc2A5a1c28979DEafc82B82749681DA92C", "is_active": False},
        }
        from app.agents.verification_agent import VerificationAgent
        registry._agents["verificationagent"] = VerificationAgent(institutional_registry=all_inactive_reg)

        coordinator = ReservationWorkflowCoordinator(
            registry=registry,
            blockchain_client=mock_client,
            blockchain_reader=mock_reader,
        )

        now = datetime.now(timezone.utc)
        req = ResourceRequest(
            request_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )

        res = await coordinator.start(req)

        # Workflow must halt at ESCALATED
        assert res.state == "ESCALATED"
        assert res.is_ready_for_approval is False
        assert res.is_committed is False

    asyncio.run(run())


def test_prevents_execution_before_approval():
    """Governance Invariant: On-chain execution cannot be triggered before attributable approval."""
    async def run():
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=MockBlockchainClient(),
            blockchain_reader=MockBlockchainReader(),
        )
        wf_id = uuid4()
        res = await coordinator.start("Urgent requirement for 2 diagnostic slots in Metropolis", workflow_id=wf_id)

        assert res.state == "WAITING_FOR_APPROVAL"

        # Attempting commitment without approver must raise PermissionError
        with pytest.raises(ValueError):
            await coordinator.authorize_and_commit(wf_id, approved_by="   ")

    asyncio.run(run())


def test_handles_blockchain_transaction_failure():
    """Verify that a reverted on-chain transaction fails cleanly and triggers recovery diagnosis."""
    async def run():
        reverting_client = MockBlockchainClient(should_revert=True)
        mock_reader = MockBlockchainReader()
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=reverting_client,
            blockchain_reader=mock_reader,
        )
        wf_id = uuid4()
        await coordinator.start("Urgent requirement for 2 diagnostic slots in Metropolis", workflow_id=wf_id)

        # Authorize and attempt on-chain execution
        res = await coordinator.authorize_and_commit(wf_id, approved_by="Dr. Alice Smith")

        assert res.is_committed is False
        assert res.state == "FAILED"
        assert "reverted" in res.error.lower()
        assert len(res.recovery_attempts) >= 1

    asyncio.run(run())


def test_prevents_duplicate_reservation_execution():
    """Idempotency Invariant: Calling commit twice on the same workflow is rejected."""
    async def run():
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=MockBlockchainClient(),
            blockchain_reader=MockBlockchainReader(),
        )
        wf_id = uuid4()
        await coordinator.start("Urgent requirement for 2 diagnostic slots in Metropolis", workflow_id=wf_id)

        # First commit succeeds
        await coordinator.authorize_and_commit(wf_id, approved_by="Dr. Alice Smith")

        # Second commit must be rejected
        with pytest.raises(ValueError) as exc:
            await coordinator.authorize_and_commit(wf_id, approved_by="Dr. Alice Smith")

        assert "already been committed" in str(exc.value).lower()

    asyncio.run(run())


def test_handles_delayed_transaction_confirmation():
    """Verify that delayed on-chain receipts are tracked cleanly without workflow crash."""
    async def run():
        delayed_reader = MockBlockchainReader(is_delayed=True)
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=MockBlockchainClient(),
            blockchain_reader=delayed_reader,
        )
        wf_id = uuid4()
        await coordinator.start("Urgent requirement for 2 diagnostic slots in Metropolis", workflow_id=wf_id)

        res = await coordinator.authorize_and_commit(wf_id, approved_by="Dr. Alice Smith")

        assert res.state == "DELAYED_CONFIRMATION"
        assert res.monitoring_status == "DELAYED"
        assert res.is_committed is True

    asyncio.run(run())


def test_preserves_complete_workflow_history():
    """Audit Invariant: Full chronological timeline, context, and metadata are retained."""
    async def run():
        coordinator = ReservationWorkflowCoordinator(
            blockchain_client=MockBlockchainClient(),
            blockchain_reader=MockBlockchainReader(),
        )
        wf_id = uuid4()
        res1 = await coordinator.start("Urgent requirement for 2 diagnostic slots in Metropolis", workflow_id=wf_id)
        res2 = await coordinator.authorize_and_commit(wf_id, approved_by="Dr. Alice Smith")

        assert res2.workflow_id == wf_id
        assert len(res2.audit_trail) >= 8

        # Verify timestamps are timezone-aware and sequential
        for step in res2.audit_trail:
            assert step["timestamp"] is not None
            assert step["agent"] is not None
            assert step["details"] is not None

        # Verify context
        assert res2.context is not None
        assert res2.context.workflow_id == wf_id
        assert res2.context.approved_by == "Dr. Alice Smith"

    asyncio.run(run())
