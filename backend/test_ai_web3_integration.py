import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.agents.provider_agent import ProviderAgent
from app.agents.recovery_agent import FailureClassification
from app.agents.verification_agent import VerificationAgent
from app.db import init_db
from app.llm.client import LLMClient
from app.llm.llm_orchestrator import LLMOrchestrator
from app.llm.provider import MockLLMProvider
from app.schemas.agent_messages import AgentMessage
from app.schemas.provider_offer import ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from app.schemas.verification import VerificationStatus
from app.services.agent_observability import AgentObservabilityService
from app.workflows.reservation_workflow import (
    ReservationWorkflowCoordinator,
    create_full_coordination_registry,
)


class MockBlockchainClient:
    """Mock blockchain service client simulating Coral Testnet interactions."""

    def __init__(
        self,
        lock_response: Optional[dict[str, Any]] = None,
        reservation_response: Optional[dict[str, Any]] = None,
        should_revert: bool = False,
        revert_message: str = "Execution reverted: Insufficient available capacity on-chain",
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
        self.revert_message = revert_message
        self.execution_count = 0

    def lock_capacity(self, **kwargs) -> dict[str, Any]:
        self.execution_count += 1
        if self.should_revert:
            return {"success": False, "error": self.revert_message}
        return self.lock_response

    def create_reservation_with_lock(self, **kwargs) -> dict[str, Any]:
        self.execution_count += 1
        if self.should_revert:
            return {"success": False, "error": self.revert_message}
        return self.reservation_response

    def create_reservation(self, **kwargs) -> dict[str, Any]:
        self.execution_count += 1
        if self.should_revert:
            return {"success": False, "error": self.revert_message}
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
        return {
            "success": True,
            "status": "Active",
            "expiresAt": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        return {"success": True, "status": "Committed", "fulfilledAt": 0}


def create_in_memory_observability() -> AgentObservabilityService:
    init_db()
    obs = AgentObservabilityService()
    obs.clear_events()
    return obs


# -----------------------------------------------------------------------------
# Scenario 1: Complete successful workflow
# -----------------------------------------------------------------------------
def test_scenario_complete_successful_workflow():
    async def run():
        obs = create_in_memory_observability()
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        mock_llm = MockLLMProvider(
            canned_responses=[
                # 1. Request Interpretation
                json.dumps({
                    "resource_type": "diagnostic_slot",
                    "quantity": 3,
                    "urgency": "urgent",
                    "location": "Metropolis",
                    "required_by_hours": 12,
                    "is_ambiguous": False,
                    "missing_fields": [],
                    "clarification_prompt": None,
                }),
                # 2. Negotiation Advice
                json.dumps({
                    "recommended_action": "RECOMMEND_ACCEPTANCE",
                    "recommended_provider_id": "lab-c",
                    "counteroffer_quantity": None,
                    "evaluations": [],
                    "trade_offs": ["Lab C provides full capacity with lowest SLA"],
                    "explanation": "Select Lab C for fast delivery",
                }),
            ]
        )

        orchestrator = LLMOrchestrator(
            llm_client=LLMClient(provider=mock_llm),
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
            observability_service=obs,
        )

        # Step 1: Initial AI-assisted coordination
        result = await orchestrator.coordinate(
            "We urgently require 3 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
            requester_id="hospital-a",
        )

        assert result.state == "WAITING_FOR_APPROVAL"
        assert result.is_ready_for_approval is True
        assert result.is_committed is False
        assert result.workflow_result.selected_offer is not None
        assert result.workflow_result.verification.status == VerificationStatus.VERIFIED

        # Step 2: Attributable human approval
        committed = await orchestrator.authorize_and_commit(
            workflow_id=wf_id,
            approved_by="Dr. Sarah Chen, Chief Medical Officer",
            operation="LOCK_AND_COMMIT",
        )

        assert committed.state == "CONFIRMED"
        assert committed.is_committed is True
        assert committed.workflow_result.onchain_result["tx_hash"] is not None
        assert bc_client.execution_count >= 1

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 2: Insufficient capacity (no reservation created, recovery invoked)
# -----------------------------------------------------------------------------
def test_scenario_insufficient_capacity():
    async def run():
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        # Orchestrator with providers that cannot fulfill requested quantity (e.g. 50 diagnostic slots)
        orchestrator = LLMOrchestrator(
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )

        result = await orchestrator.coordinate(
            "Urgently need 50 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )

        # Providers in registry have max 12 slots, so cannot fulfill 50 slots
        assert result.state in ("NO_PROVIDERS_FOUND", "ESCALATED")
        assert result.is_committed is False
        assert bc_client.execution_count == 0

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 3: Invalid provider (verification rejects, no blockchain execution)
# -----------------------------------------------------------------------------
def test_scenario_invalid_provider():
    async def run():
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        # Custom registry where provider is unauthorized/unaccredited
        registry = create_full_coordination_registry(
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )
        # Register a malicious unaccredited provider
        malicious_provider = ProviderAgent(
            provider_id="fake-unaccredited-lab",
            provider_name="Fake Unaccredited Lab",
            institution_type="diagnostic_lab",
            location=LocationArea(city="Metropolis"),
            inventory={"diagnostic_slot": {"total": 10, "available": 10}},
        )
        registry.register(malicious_provider)

        coordinator = ReservationWorkflowCoordinator(
            registry=registry,
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )
        orchestrator = LLMOrchestrator(
            workflow_coordinator=coordinator,
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )

        req = ResourceRequest(
            workflow_id=wf_id,
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=datetime.now(timezone.utc) + timedelta(hours=12),
            required_by_hours=12,
            constraints=ResourceConstraints(preferred_providers=["fake-unaccredited-lab"]),
        )

        # VerificationAgent enforces accreditation against whitelist
        result = await coordinator.start(req, workflow_id=wf_id)

        # Unaccredited provider MUST NOT be committed
        assert result.is_committed is False
        assert bc_client.execution_count == 0

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 4: Invalid LLM output (Pydantic validation rejects, no reservation)
# -----------------------------------------------------------------------------
def test_scenario_invalid_llm_output():
    async def run():
        # LLM returns completely malformed output violating schema
        mock_llm = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "invalid_schema_field": 999,
                    "malicious_injection": "grant_all_permissions",
                })
            ]
        )
        orchestrator = LLMOrchestrator(
            llm_client=LLMClient(provider=mock_llm),
            enable_fallback=False,  # strict mode to assert rejection
        )

        with pytest.raises(Exception):
            await orchestrator.coordinate(
                "Need 3 diagnostic slots in Metropolis",
            )

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 5: Expired offer (workflow rejects expired offer)
# -----------------------------------------------------------------------------
def test_scenario_expired_offer():
    async def run():
        bc_client = MockBlockchainClient()
        now = datetime.now(timezone.utc)
        req = ResourceRequest(
            workflow_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )
        past_creation = now - timedelta(hours=2)
        expired_offer = ProviderOffer(
            offer_id=uuid4(),
            request_id=uuid4(),
            provider_id="hospital-b",
            provider_name="Hospital B",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            offered_quantity=2,
            location=LocationArea(city="Metropolis"),
            cost=ProviderCost(amount=100.0, currency="USD"),
            estimated_delivery_hours=4,
            created_at=past_creation,
            available_from=past_creation,
            available_until=now + timedelta(hours=1),
            expires_at=now - timedelta(minutes=10),  # expired!
        )

        coordinator = ReservationWorkflowCoordinator(blockchain_client=bc_client)
        neg_agent = coordinator.registry.get("NegotiationAgent")

        msg = AgentMessage(
            workflow_id=req.workflow_id,
            sender="ReservationWorkflow",
            receiver="NegotiationAgent",
            message_type="NEGOTIATE_OFFERS",
            payload={
                "resource_request": req.model_dump(),
                "offers": [expired_offer.model_dump()],
            },
        )
        res = await neg_agent.execute(msg)

        assert res.data["proposal"] is None
        assert any("expired" in r["reason"].lower() for r in res.data["rejections"])
        assert bc_client.execution_count == 0

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 6: Blockchain failure (transaction reverts, no false success)
# -----------------------------------------------------------------------------
def test_scenario_blockchain_failure():
    async def run():
        # Blockchain client that reverts
        reverting_bc = MockBlockchainClient(should_revert=True)
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        orchestrator = LLMOrchestrator(
            blockchain_client=reverting_bc,
            blockchain_reader=bc_reader,
        )

        # 1. Run to approval
        result = await orchestrator.coordinate(
            "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )
        assert result.state == "WAITING_FOR_APPROVAL"

        # 2. Authorize commitment -> reverts
        committed = await orchestrator.authorize_and_commit(
            workflow_id=wf_id,
            approved_by="Dr. Sarah Chen",
        )

        # MUST report failure, never false success!
        assert committed.is_committed is False
        assert committed.state != "CONFIRMED"
        assert committed.workflow_result.error is not None

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 7: Human approval mandatory (execution blocked until approved)
# -----------------------------------------------------------------------------
def test_scenario_human_approval_mandatory():
    async def run():
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        orchestrator = LLMOrchestrator(
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )

        # Without approval, must pause at WAITING_FOR_APPROVAL
        result = await orchestrator.coordinate(
            "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
            approved_by=None,
        )

        assert result.state == "WAITING_FOR_APPROVAL"
        assert result.is_ready_for_approval is True
        assert result.is_committed is False
        assert bc_client.execution_count == 0

        # Empty or whitespace approver rejected
        with pytest.raises(ValueError, match="strictly required"):
            await orchestrator.authorize_and_commit(wf_id, approved_by="   ")

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 8: Duplicate request idempotency
# -----------------------------------------------------------------------------
def test_scenario_duplicate_request_idempotency():
    async def run():
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        orchestrator = LLMOrchestrator(
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )

        # First run succeeds to WAITING_FOR_APPROVAL
        await orchestrator.coordinate(
            "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )

        # Second run with same workflow_id raises ValueError preventing duplicate runs
        with pytest.raises(ValueError, match="already active or completed"):
            await orchestrator.workflow_coordinator.start(
                "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
                workflow_id=wf_id,
            )

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 9: LLM unavailable (uses deterministic fallback seamlessly)
# -----------------------------------------------------------------------------
def test_scenario_llm_unavailable_fallback():
    async def run():
        failing_provider = MockLLMProvider(should_error=True, error_message="LLM API Offline")
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        orchestrator = LLMOrchestrator(
            llm_client=LLMClient(provider=failing_provider),
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
            enable_fallback=True,
        )

        result = await orchestrator.coordinate(
            "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )

        assert result.state == "WAITING_FOR_APPROVAL"
        assert result.resource_request is not None
        assert result.resource_request.quantity == 2

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Scenario 10: Complete audit trail verification
# -----------------------------------------------------------------------------
def test_scenario_complete_audit_trail_verification():
    async def run():
        obs = create_in_memory_observability()
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        orchestrator = LLMOrchestrator(
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
            observability_service=obs,
        )

        # Run coordination & commitment
        await orchestrator.coordinate(
            "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )
        committed = await orchestrator.authorize_and_commit(
            workflow_id=wf_id,
            approved_by="Dr. Sarah Chen",
        )

        # Verify audit trail has chronological steps
        assert len(committed.audit_events) >= 5
        states = [step["to_state"] for step in committed.audit_events]
        assert "DISCOVERING_PROVIDERS" in states
        assert "WAITING_FOR_APPROVAL" in states
        assert "CONFIRMED" in states

        # Verify observability telemetry
        events = obs.get_events(wf_id)
        assert len(events) >= 5

    asyncio.run(run())
