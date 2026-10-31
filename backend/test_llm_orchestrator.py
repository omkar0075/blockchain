import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.db import init_db
from app.llm.client import LLMClient
from app.llm.llm_orchestrator import LLMOrchestrationResult, LLMOrchestrator
from app.llm.negotiation_advisor import LLMNegotiationAdvisor
from app.llm.provider import MockLLMProvider
from app.llm.recovery_advisor import LLMRecoveryAdvisor
from app.llm.request_interpreter import LLMRequestParser
from app.llm.schemas import (
    NegotiationAdviceOutput,
    RecoveryAdviceOutput,
    RequestInterpretationOutput,
)
from app.schemas.provider_offer import ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from app.services.agent_observability import AgentObservabilityService


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


def create_sample_offer(
    provider_id: str,
    provider_name: str,
    quantity: int = 3,
    delivery_hours: int = 4,
    cost_amount: float = 120.0,
) -> ProviderOffer:
    now = datetime.now(timezone.utc)
    return ProviderOffer(
        offer_id=uuid4(),
        request_id=uuid4(),
        provider_id=provider_id,
        provider_name=provider_name,
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        offered_quantity=quantity,
        location=LocationArea(city="Metropolis"),
        cost=ProviderCost(amount=cost_amount, currency="USD"),
        estimated_delivery_hours=delivery_hours,
        available_from=now,
        available_until=now + timedelta(hours=24),
        expires_at=now + timedelta(hours=6),
    )


# -----------------------------------------------------------------------------
# Test 1: Integrates Request Interpreter
# -----------------------------------------------------------------------------
def test_integrates_request_interpreter():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "resource_type": "diagnostic_slot",
                    "quantity": 3,
                    "urgency": "urgent",
                    "location": "Metropolis",
                    "required_by_hours": 12,
                    "is_ambiguous": False,
                    "missing_fields": [],
                    "clarification_prompt": None,
                })
            ]
        )
        orchestrator = LLMOrchestrator(llm_client=LLMClient(provider=mock_provider))

        interp, req = await orchestrator.interpret_request(
            "We urgently require 3 diagnostic slots in Metropolis within 12 hours",
            requester_id="hospital-a",
        )

        assert isinstance(interp, RequestInterpretationOutput)
        assert interp.resource_type == "diagnostic_slot"
        assert interp.quantity == 3
        assert interp.location == "Metropolis"
        assert interp.urgency == "urgent"
        assert req is not None
        assert req.resource_type == ResourceType.DIAGNOSTIC_SLOT
        assert req.quantity == 3

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 2: Integrates Negotiation Advisor
# -----------------------------------------------------------------------------
def test_integrates_negotiation_advisor():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "recommended_action": "RECOMMEND_ACCEPTANCE",
                    "recommended_provider_id": "lab-c",
                    "counteroffer_quantity": None,
                    "evaluations": [
                        {
                            "provider_id": "hospital-b",
                            "strengths": ["Adequate capacity"],
                            "weaknesses": ["Higher cost"],
                            "meets_hard_constraints": True,
                        },
                        {
                            "provider_id": "lab-c",
                            "strengths": ["Faster SLA", "Lower cost"],
                            "weaknesses": [],
                            "meets_hard_constraints": True,
                        },
                    ],
                    "trade_offs": ["Lab C is 2 hours faster than Hospital B."],
                    "explanation": "Recommend Lab C for best speed and price.",
                })
            ]
        )
        orchestrator = LLMOrchestrator(llm_client=LLMClient(provider=mock_provider))

        req = ResourceRequest(
            workflow_id=uuid4(),
            requester_institution_id="hospital-a",
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=3,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Metropolis"),
            required_by=datetime.now(timezone.utc) + timedelta(hours=12),
            required_by_hours=12,
        )
        offer_b = create_sample_offer("hospital-b", "Hospital B", quantity=3, delivery_hours=4, cost_amount=150.0)
        offer_c = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=120.0)

        advice = await orchestrator.advise_negotiation(req, [offer_b, offer_c])

        assert isinstance(advice, NegotiationAdviceOutput)
        assert advice.recommended_action == "RECOMMEND_ACCEPTANCE"
        assert advice.recommended_provider_id == "lab-c"
        assert len(advice.evaluations) == 2

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 3: Integrates Recovery Advisor
# -----------------------------------------------------------------------------
def test_integrates_recovery_advisor():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "failure_classification": "PROVIDER_REJECTION",
                    "root_cause_explanation": "Provider declined due to scheduled equipment overhaul.",
                    "suggested_recovery_path": "FIND_ALTERNATIVE_PROVIDER",
                    "is_recoverable": True,
                    "requires_human_intervention": False,
                    "alternative_provider_ids": ["lab-c"],
                    "suggested_counteroffer_quantity": None,
                    "remediation_steps": ["Exclude failed provider", "Seek candidate lab-c"],
                })
            ]
        )
        orchestrator = LLMOrchestrator(llm_client=LLMClient(provider=mock_provider))

        payload = {
            "error": "Provider Hospital-B explicitly declined: maintenance overhaul",
            "failed_provider": "hospital-b",
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["hospital-b", "lab-c"],
        }
        advice = await orchestrator.advise_recovery(payload)

        assert isinstance(advice, RecoveryAdviceOutput)
        assert advice.failure_classification == "PROVIDER_REJECTION"
        assert advice.suggested_recovery_path == "FIND_ALTERNATIVE_PROVIDER"
        assert "hospital-b" not in advice.alternative_provider_ids
        assert "lab-c" in advice.alternative_provider_ids

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 4: Preserves workflow ID across all operations
# -----------------------------------------------------------------------------
def test_preserves_workflow_id():
    async def run():
        wf_id = uuid4()
        orchestrator = LLMOrchestrator()  # Deterministic engines

        result = await orchestrator.coordinate(
            "Urgently need 3 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )

        assert result.workflow_id == wf_id
        if result.resource_request:
            assert result.resource_request.workflow_id == wf_id
        if result.workflow_result:
            assert result.workflow_result.workflow_id == wf_id

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 5: Validates all LLM outputs through Pydantic
# -----------------------------------------------------------------------------
def test_validates_all_llm_outputs():
    async def run():
        orchestrator = LLMOrchestrator()

        result = await orchestrator.coordinate(
            "Urgently need 3 diagnostic slots in Metropolis within 12 hours"
        )

        assert isinstance(result, LLMOrchestrationResult)
        assert isinstance(result.interpretation, RequestInterpretationOutput)
        if result.negotiation_advice:
            assert isinstance(result.negotiation_advice, NegotiationAdviceOutput)

        # Ensure schemas forbid unknown extra properties
        with pytest.raises(Exception):
            RequestInterpretationOutput.model_validate({"unexpected_bogus_key": 123})

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 6: Handles LLM provider failure gracefully
# -----------------------------------------------------------------------------
def test_handles_llm_provider_failure():
    async def run():
        # Provider simulates 502 Bad Gateway / upstream outage
        failing_provider = MockLLMProvider(should_error=True, error_message="LLM API Gateway Outage")
        orchestrator = LLMOrchestrator(
            llm_client=LLMClient(provider=failing_provider),
            enable_fallback=True,
        )

        # Must not raise unhandled exception; must complete using fallback
        result = await orchestrator.coordinate(
            "Urgently need 3 diagnostic slots in Metropolis within 12 hours"
        )

        assert result.state in ("WAITING_FOR_APPROVAL", "NEEDS_CLARIFICATION", "CONFIRMED")
        assert result.interpretation is not None
        assert result.resource_request is not None

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 7: Uses deterministic fallbacks
# -----------------------------------------------------------------------------
def test_uses_deterministic_fallbacks():
    async def run():
        failing_provider = MockLLMProvider(should_error=True, error_message="LLM Unavailable")
        orchestrator = LLMOrchestrator(
            llm_client=LLMClient(provider=failing_provider),
            enable_fallback=True,
        )

        # 1. Fallback Request Interpretation
        interp, req = await orchestrator.interpret_request(
            "Urgently need 3 diagnostic slots in Metropolis within 8 hours",
            requester_id="hospital-a",
        )
        assert interp.resource_type == "diagnostic_slot"
        assert req is not None
        assert req.quantity == 3

        # 2. Fallback Negotiation Advice
        offer_b = create_sample_offer("hospital-b", "Hospital B", quantity=3, delivery_hours=4, cost_amount=150.0)
        offer_c = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=120.0)
        neg_advice = await orchestrator.advise_negotiation(req, [offer_b, offer_c])
        assert isinstance(neg_advice, NegotiationAdviceOutput)
        assert neg_advice.recommended_provider_id == "lab-c"

        # 3. Fallback Recovery Advice
        rec_advice = await orchestrator.advise_recovery({
            "error": "Temporary RPC connection timeout to node",
            "attempt": 1,
            "max_attempts": 3,
        })
        assert isinstance(rec_advice, RecoveryAdviceOutput)
        assert rec_advice.suggested_recovery_path == "RETRY_WITH_BACKOFF"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 8: Records LLM recommendations in observability logs
# -----------------------------------------------------------------------------
def test_records_llm_recommendations():
    async def run():
        obs_service = create_in_memory_observability()
        wf_id = uuid4()
        orchestrator = LLMOrchestrator(
            observability_service=obs_service,
        )

        result = await orchestrator.coordinate(
            "Urgently need 3 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )

        # Query events from database
        events = obs_service.get_events(wf_id)
        event_types = [e["event_type"] for e in events]

        assert "LLM_REQUEST_INTERPRETATION_COMPLETED" in event_types
        assert "COORDINATION_STARTED" in event_types

        # Verify all events are correlated by the same workflow_id
        for e in events:
            assert e["workflow_id"] == str(wf_id)

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 9: Prevents unauthorized blockchain execution
# -----------------------------------------------------------------------------
def test_prevents_unauthorized_blockchain_execution():
    async def run():
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        orchestrator = LLMOrchestrator(
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )

        # 1. Advisory coordination without human approval MUST pause at WAITING_FOR_APPROVAL
        result = await orchestrator.coordinate(
            "Urgently need 3 diagnostic slots in Metropolis within 12 hours",
            approved_by=None,
        )

        assert result.state == "WAITING_FOR_APPROVAL"
        assert result.is_ready_for_approval is True
        assert result.is_committed is False
        assert result.workflow_result.onchain_result is None

        # 2. Attempting to commit without an attributable human approver MUST be rejected
        with pytest.raises(ValueError, match="strictly required"):
            await orchestrator.authorize_and_commit(
                workflow_id=result.workflow_id,
                approved_by="",
            )

        # 3. LLM orchestrator does not contain private keys or raw transaction signing authority
        assert not hasattr(orchestrator, "private_key")
        assert not hasattr(orchestrator, "sign_transaction")

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 10: Completes AI-assisted coordination end-to-end
# -----------------------------------------------------------------------------
def test_completes_ai_assisted_coordination():
    async def run():
        obs_service = create_in_memory_observability()
        bc_client = MockBlockchainClient()
        bc_reader = MockBlockchainReader()
        wf_id = uuid4()

        orchestrator = LLMOrchestrator(
            observability_service=obs_service,
            blockchain_client=bc_client,
            blockchain_reader=bc_reader,
        )

        # 1. Coordinate up to human approval
        initial_result = await orchestrator.coordinate(
            "We urgently require 3 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
            requester_id="hospital-a",
        )

        assert initial_result.state == "WAITING_FOR_APPROVAL"
        assert initial_result.is_ready_for_approval is True
        assert initial_result.resource_request.quantity == 3
        assert initial_result.workflow_result.selected_offer is not None

        # 2. Provide attributable institutional approval to execute on-chain
        committed_result = await orchestrator.authorize_and_commit(
            workflow_id=wf_id,
            approved_by="Dr. Sarah Chen, Chief Medical Officer",
            operation="LOCK_AND_COMMIT",
        )

        assert committed_result.state == "CONFIRMED"
        assert committed_result.is_committed is True
        assert committed_result.workflow_result.onchain_result is not None
        assert "tx_hash" in committed_result.workflow_result.onchain_result

        # Verify audit trail and observability events
        assert len(committed_result.audit_events) >= 5
        events = obs_service.get_events(wf_id)
        assert len(events) >= 5

    asyncio.run(run())
