import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest

from app.agents.negotiation_agent import NegotiationAgent
from app.agents.recovery_agent import RecoveryAgent
from app.agents.request_agent import RequestAgent
from app.db import init_db
from app.llm.client import LLMClient
from app.llm.provider import MockLLMProvider
from app.llm.schemas import (
    NegotiationAdviceOutput,
    RecoveryAdviceOutput,
    RequestInterpretationOutput,
)
from app.schemas.agent_messages import AgentMessage
from app.schemas.provider_offer import ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from app.services.agent_llm_bridge import AIRecommendation, AgentLLMBridge
from app.services.agent_observability import AgentObservabilityService


def create_in_memory_observability() -> AgentObservabilityService:
    init_db()
    obs = AgentObservabilityService()
    obs.clear_events()
    return obs


def create_sample_request(quantity: int = 3) -> ResourceRequest:
    return ResourceRequest(
        workflow_id=uuid4(),
        requester_institution_id="hospital-a",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=quantity,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city="Metropolis"),
        required_by=datetime.now(timezone.utc) + timedelta(hours=12),
        required_by_hours=12,
    )


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
# Test 1: Connects Request Agent to LLM Interpreter
# -----------------------------------------------------------------------------
def test_connects_request_agent_to_llm_interpreter():
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
        bridge = AgentLLMBridge(llm_client=LLMClient(provider=mock_provider))

        rec = await bridge.interpret_request(
            "We urgently require 3 diagnostic slots in Metropolis within 12 hours",
            requester_id="hospital-a",
        )

        assert isinstance(rec, AIRecommendation)
        assert rec.agent_name == "RequestAgent"
        assert rec.capability == "RequestInterpreter"
        assert rec.is_ai_assisted is True
        assert rec.data["quantity"] == 3
        assert rec.data["resource_type"] == "diagnostic_slot"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 2: Connects Negotiation Agent to LLM Advisor
# -----------------------------------------------------------------------------
def test_connects_negotiation_agent_to_llm_advisor():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "recommended_action": "RECOMMEND_ACCEPTANCE",
                    "recommended_provider_id": "lab-c",
                    "counteroffer_quantity": None,
                    "evaluations": [],
                    "trade_offs": ["Fastest turnaround."],
                    "explanation": "Lab C is recommended for speed.",
                })
            ]
        )
        bridge = AgentLLMBridge(llm_client=LLMClient(provider=mock_provider))
        req = create_sample_request(3)
        offer_b = create_sample_offer("hospital-b", "Hospital B", quantity=3, delivery_hours=4)
        offer_c = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2)

        rec = await bridge.advise_negotiation(req, [offer_b, offer_c])

        assert isinstance(rec, AIRecommendation)
        assert rec.agent_name == "NegotiationAgent"
        assert rec.capability == "NegotiationAdvisor"
        assert rec.is_ai_assisted is True
        assert rec.data["recommended_provider_id"] == "lab-c"
        assert rec.requires_human_approval is True

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 3: Connects Recovery Agent to LLM Advisor
# -----------------------------------------------------------------------------
def test_connects_recovery_agent_to_llm_advisor():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "failure_classification": "PROVIDER_REJECTION",
                    "root_cause_explanation": "Provider declined due to equipment servicing.",
                    "suggested_recovery_path": "FIND_ALTERNATIVE_PROVIDER",
                    "is_recoverable": True,
                    "requires_human_intervention": False,
                    "alternative_provider_ids": ["lab-c"],
                    "suggested_counteroffer_quantity": None,
                    "remediation_steps": ["Exclude Hospital B", "Consult Lab C"],
                })
            ]
        )
        bridge = AgentLLMBridge(llm_client=LLMClient(provider=mock_provider))
        payload = {
            "error": "Provider declined: servicing in progress",
            "failed_provider": "hospital-b",
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["hospital-b", "lab-c"],
        }

        rec = await bridge.advise_recovery(payload)

        assert isinstance(rec, AIRecommendation)
        assert rec.agent_name == "RecoveryAgent"
        assert rec.capability == "RecoveryAdvisor"
        assert rec.is_ai_assisted is True
        assert rec.data["suggested_recovery_path"] == "FIND_ALTERNATIVE_PROVIDER"
        assert "hospital-b" not in rec.data["alternative_provider_ids"]

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 4: Passes only validated, minimum-required data (no PII or credentials)
# -----------------------------------------------------------------------------
def test_passes_only_validated_data():
    async def run():
        bridge = AgentLLMBridge()
        req = create_sample_request(3)

        # Candidate with private fields and credentials
        candidates = [
            {
                "provider_id": "lab-c",
                "location": "Metropolis",
                "private_key": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
                "patient_name": "John Doe",
                "ssn": "123-45-6789",
            }
        ]

        rec = await bridge.explain_provider_suitability(req, candidates)

        # Private keys and PII must be completely stripped from output data
        matched = rec.data["matched_candidates"][0]
        assert "private_key" not in matched
        assert "patient_name" not in matched
        assert "ssn" not in matched
        assert matched["provider_id"] == "lab-c"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 5: Preserves workflow context
# -----------------------------------------------------------------------------
def test_preserves_workflow_context():
    async def run():
        bridge = AgentLLMBridge()
        wf_id = uuid4()
        req = create_sample_request(3)
        req.workflow_id = wf_id

        rec1 = await bridge.interpret_request("Need 3 diagnostic slots in Metropolis", workflow_id=wf_id)
        assert rec1.workflow_id == wf_id

        rec2 = await bridge.advise_negotiation(req, [], workflow_id=wf_id)
        assert rec2.workflow_id == wf_id

        rec3 = await bridge.advise_recovery({"error": "test"}, workflow_id=wf_id)
        assert rec3.workflow_id == wf_id

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 6: Rejects invalid LLM recommendations
# -----------------------------------------------------------------------------
def test_rejects_invalid_llm_recommendations():
    async def run():
        # LLM returns invalid strategy
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "failure_classification": "PROVIDER_REJECTION",
                    "root_cause_explanation": "Declined.",
                    "suggested_recovery_path": "INVALID_HALLUCINATED_PATH",
                    "is_recoverable": True,
                    "requires_human_intervention": False,
                    "alternative_provider_ids": ["lab-c"],
                    "suggested_counteroffer_quantity": None,
                    "remediation_steps": [],
                })
            ]
        )
        bridge = AgentLLMBridge(llm_client=LLMClient(provider=mock_provider))

        rec = await bridge.advise_recovery({
            "error": "Declined",
            "failed_provider": "hospital-b",
            "known_providers": ["hospital-b", "lab-c"],
        })

        # Sanitizer rejects hallucinated strategy and maps to valid deterministic strategy
        assert rec.data["suggested_recovery_path"] in (
            "FIND_ALTERNATIVE_PROVIDER",
            "RE_NEGOTIATE",
            "RETRY_WITH_BACKOFF",
            "RELEASE_LOCK_AND_RETRY",
            "INITIATE_DISPUTE",
            "ESCALATE_TO_HUMAN",
            "ABORT_UNRECOVERABLE",
        )

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 7: Uses deterministic fallback
# -----------------------------------------------------------------------------
def test_uses_deterministic_fallback():
    async def run():
        # LLM provider fails completely
        mock_provider = MockLLMProvider(should_error=True, error_message="LLM Offline")
        bridge = AgentLLMBridge(
            llm_client=LLMClient(provider=mock_provider),
            enable_fallback=True,
        )

        rec = await bridge.interpret_request(
            "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
        )

        assert rec.used_fallback is True
        assert rec.is_ai_assisted is False
        assert rec.data["resource_type"] == "diagnostic_slot"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 8: Records AI-assisted decisions in observability
# -----------------------------------------------------------------------------
def test_records_ai_assisted_decisions():
    async def run():
        obs = create_in_memory_observability()
        bridge = AgentLLMBridge(observability_service=obs)
        wf_id = uuid4()

        rec = await bridge.interpret_request(
            "Urgently need 2 diagnostic slots in Metropolis within 12 hours",
            workflow_id=wf_id,
        )

        events = obs.get_events(wf_id)
        assert len(events) >= 1
        event = events[0]
        assert event["agent_id"] == "RequestAgent"
        assert event["event_type"] == "AI_REQUEST_INTERPRETATION"
        assert event["payload"]["capability"] == "RequestInterpreter"
        assert "is_ai_assisted" in event["payload"]

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 9: Deterministic policy overrides LLM recommendations
# -----------------------------------------------------------------------------
def test_deterministic_policy_overrides_llm():
    async def run():
        # LLM recommends provider hospital-b with cost $500, but request has budget $200
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "recommended_action": "RECOMMEND_ACCEPTANCE",
                    "recommended_provider_id": "hospital-b",
                    "counteroffer_quantity": None,
                    "evaluations": [],
                    "trade_offs": [],
                    "explanation": "Recommend Hospital B.",
                })
            ]
        )
        bridge = AgentLLMBridge(llm_client=LLMClient(provider=mock_provider))

        req = create_sample_request(3)
        req.constraints = ResourceConstraints(specifications={"max_cost": 200.0})
        expensive_offer = create_sample_offer("hospital-b", "Hospital B", cost_amount=500.0)

        # Deterministic NegotiationAgent evaluates offer
        agent = NegotiationAgent(advisor=bridge.negotiation_advisor)
        msg = AgentMessage(
            workflow_id=req.workflow_id,
            sender="Orchestrator",
            receiver="NegotiationAgent",
            message_type="NEGOTIATE_OFFERS",
            payload={
                "resource_request": req.model_dump(),
                "offers": [expensive_offer.model_dump()],
                "max_budget": 200.0,
            },
        )
        res = await agent.execute(msg)

        # Deterministic policy strictly overrides: cost exceeds budget, so rejected!
        assert res.data["proposal"] is None
        assert any("exceeds budget" in r["reason"] for r in res.data["rejections"])

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 10: Prevents LLM privileged blockchain actions
# -----------------------------------------------------------------------------
def test_prevents_llm_privileged_blockchain_actions():
    bridge = AgentLLMBridge()
    # Enforces absence of blockchain signing privileges on LLM layer
    bridge.assert_no_blockchain_authority()


# -----------------------------------------------------------------------------
# Test 11: Handles LLM provider timeout
# -----------------------------------------------------------------------------
def test_handles_llm_provider_timeout():
    async def run():
        # LLM times out
        mock_provider = MockLLMProvider(should_timeout=True)
        bridge = AgentLLMBridge(
            llm_client=LLMClient(provider=mock_provider, timeout=0.1, max_retries=0),
            enable_fallback=True,
        )

        rec = await bridge.interpret_request(
            "Urgently need 3 diagnostic slots in Metropolis within 12 hours",
        )

        assert rec.used_fallback is True
        assert rec.is_ai_assisted is False
        assert rec.data["quantity"] is not None

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 12: Human approval remains mandatory for sensitive decisions
# -----------------------------------------------------------------------------
def test_human_approval_remains_mandatory():
    async def run():
        bridge = AgentLLMBridge()
        req = create_sample_request(3)
        offer = create_sample_offer("hospital-b", "Hospital B", quantity=3)

        rec = await bridge.advise_negotiation(req, [offer])

        # Sensitive actions (accepting provider offers) MUST require human approval
        assert rec.requires_human_approval is True

    asyncio.run(run())
