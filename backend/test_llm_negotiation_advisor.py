import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest

from app.agents.negotiation_agent import NegotiationAgent
from app.llm.client import LLMClient
from app.llm.negotiation_advisor import LLMNegotiationAdvisor
from app.llm.provider import MockLLMProvider
from app.schemas.agent_messages import AgentMessage, AgentStatus
from app.schemas.negotiation import NegotiationStatus
from app.schemas.provider_offer import ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)


def create_sample_request(
    quantity: int = 3,
    deadline_hours: int = 12,
    max_budget: float = 200.0,
) -> ResourceRequest:
    now = datetime.now(timezone.utc)
    return ResourceRequest(
        request_id=uuid4(),
        requester_institution_id="hospital-a",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=quantity,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city="Metropolis"),
        required_by=now + timedelta(hours=deadline_hours),
        required_by_hours=deadline_hours,
        constraints=ResourceConstraints(
            specifications={"max_cost": max_budget},
        ),
    )


def create_sample_offer(
    provider_id: str,
    provider_name: str,
    quantity: int = 3,
    delivery_hours: int = 4,
    cost_amount: float = 120.0,
    expires_in_hours: int = 6,
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
        expires_at=now + timedelta(hours=expires_in_hours),
    )


# -----------------------------------------------------------------------------
# Test 1: Recommends the best matching provider
# -----------------------------------------------------------------------------
def test_recommends_best_matching_provider():
    async def run():
        request = create_sample_request(quantity=3, deadline_hours=12)
        offer_b = create_sample_offer("hospital-b", "Community Hospital B", quantity=3, delivery_hours=4, cost_amount=150.0)
        offer_c = create_sample_offer("lab-c", "Central Diagnostic Lab C", quantity=3, delivery_hours=2, cost_amount=120.0)

        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "recommended_action": "RECOMMEND_ACCEPTANCE",
                    "recommended_provider_id": "lab-c",
                    "counteroffer_quantity": None,
                    "evaluations": [
                        {
                            "provider_id": "hospital-b",
                            "strengths": ["Full quantity 3 slots"],
                            "weaknesses": ["Slower delivery (4h) and higher cost ($150)"],
                            "meets_hard_constraints": True,
                        },
                        {
                            "provider_id": "lab-c",
                            "strengths": ["Fast turnaround (2h)", "Lower cost ($120)"],
                            "weaknesses": [],
                            "meets_hard_constraints": True,
                        },
                    ],
                    "trade_offs": ["Lab C is both faster by 2h and cheaper by $30."],
                    "explanation": "Lab C provides the entire requested quantity with the fastest delivery and lowest cost.",
                })
            ]
        )

        advisor = LLMNegotiationAdvisor(llm_client=LLMClient(provider=mock_provider))
        advice = await advisor.advise_async(request, [offer_b, offer_c])

        assert advice.recommended_action == "RECOMMEND_ACCEPTANCE"
        assert advice.recommended_provider_id == "lab-c"
        assert advice.counteroffer_quantity is None
        assert "Lab C" in advice.explanation

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 2: Explains provider-selection reasoning
# -----------------------------------------------------------------------------
def test_explains_provider_selection_reasoning():
    async def run():
        request = create_sample_request(quantity=3)
        offer_b = create_sample_offer("hospital-b", "Hospital B", quantity=3, delivery_hours=6, cost_amount=160.0)
        offer_c = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=110.0)

        advisor = LLMNegotiationAdvisor()  # Uses deterministic fallback engine
        advice = await advisor.advise_async(request, [offer_b, offer_c])

        assert len(advice.evaluations) == 2
        eval_c = next(e for e in advice.evaluations if e.provider_id == "lab-c")
        eval_b = next(e for e in advice.evaluations if e.provider_id == "hospital-b")

        assert any("Fast delivery" in s for s in eval_c.strengths)
        assert eval_c.meets_hard_constraints is True
        assert eval_b.meets_hard_constraints is True
        assert advice.recommended_provider_id == "lab-c"
        assert len(advice.trade_offs) > 0

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 3: Handles partial quantity offers
# -----------------------------------------------------------------------------
def test_handles_partial_quantity_offers():
    async def run():
        request = create_sample_request(quantity=5, deadline_hours=12)
        # Provider only offers 3 out of 5 requested units
        partial_offer = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=100.0)

        advisor = LLMNegotiationAdvisor()
        advice = await advisor.advise_async(request, [partial_offer])

        assert advice.recommended_action == "SUGGEST_COUNTEROFFER"
        assert advice.recommended_provider_id == "lab-c"
        assert advice.counteroffer_quantity == 3
        assert "counteroffer" in advice.explanation.lower()

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 4: Suggests valid counteroffers (Never exceeds requested quantity)
# -----------------------------------------------------------------------------
def test_suggests_valid_counteroffers():
    async def run():
        request = create_sample_request(quantity=4)
        offer = create_sample_offer("lab-c", "Lab C", quantity=2, delivery_hours=3)

        # Mock LLM erroneously suggests counteroffer quantity of 10
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "recommended_action": "SUGGEST_COUNTEROFFER",
                    "recommended_provider_id": "lab-c",
                    "counteroffer_quantity": 10,  # Invalid: exceeds requested 4
                    "evaluations": [],
                    "trade_offs": [],
                    "explanation": "Suggest partial counteroffer.",
                })
            ]
        )
        advisor = LLMNegotiationAdvisor(llm_client=LLMClient(provider=mock_provider))
        advice = await advisor.advise_async(request, [offer])

        # Sanitization rule: counteroffer quantity is clamped to available capacity <= request.quantity
        assert advice.counteroffer_quantity <= request.quantity
        assert advice.counteroffer_quantity == 2

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 5: Respects budget constraints
# -----------------------------------------------------------------------------
def test_respects_budget_constraints():
    async def run():
        request = create_sample_request(quantity=3, max_budget=100.0)
        # Offer A exceeds budget ($150 > $100); Offer B is within budget ($90 <= $100)
        expensive_offer = create_sample_offer("hospital-b", "Hospital B", quantity=3, cost_amount=150.0, delivery_hours=2)
        budget_offer = create_sample_offer("lab-c", "Lab C", quantity=3, cost_amount=90.0, delivery_hours=4)

        # LLM tries to recommend the expensive offer
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "recommended_action": "RECOMMEND_ACCEPTANCE",
                    "recommended_provider_id": "hospital-b",
                    "counteroffer_quantity": None,
                    "evaluations": [],
                    "trade_offs": [],
                    "explanation": "Hospital B is faster despite being over budget.",
                })
            ]
        )
        advisor = LLMNegotiationAdvisor(llm_client=LLMClient(provider=mock_provider))
        advice = await advisor.advise_async(request, [expensive_offer, budget_offer], max_budget=100.0)

        # Deterministic constraint enforcement: over-budget recommendation is overridden
        assert advice.recommended_provider_id == "lab-c"
        assert advice.recommended_action == "RECOMMEND_ACCEPTANCE"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 6: Respects deadline constraints
# -----------------------------------------------------------------------------
def test_respects_deadline_constraints():
    async def run():
        request = create_sample_request(quantity=3, deadline_hours=6)
        # Late offer takes 10 hours; Fast offer takes 3 hours
        late_offer = create_sample_offer("hospital-b", "Hospital B", quantity=3, delivery_hours=10, cost_amount=80.0)
        fast_offer = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=3, cost_amount=120.0)

        advisor = LLMNegotiationAdvisor()
        advice = await advisor.advise_async(request, [late_offer, fast_offer])

        eval_late = next(e for e in advice.evaluations if e.provider_id == "hospital-b")
        assert eval_late.meets_hard_constraints is False
        assert any("deadline" in w.lower() for w in eval_late.weaknesses)

        assert advice.recommended_provider_id == "lab-c"
        assert advice.recommended_action == "RECOMMEND_ACCEPTANCE"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 7: Handles conflicting provider offers with clear trade-offs
# -----------------------------------------------------------------------------
def test_handles_conflicting_provider_offers():
    async def run():
        request = create_sample_request(quantity=3, deadline_hours=12, max_budget=250.0)
        # Offer 1: Fast (2h) but expensive ($220)
        fast_costly = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=220.0)
        # Offer 2: Slower (6h) but economical ($100)
        slow_cheap = create_sample_offer("hospital-b", "Hospital B", quantity=3, delivery_hours=6, cost_amount=100.0)

        advisor = LLMNegotiationAdvisor()
        advice = await advisor.advise_async(request, [fast_costly, slow_cheap])

        assert len(advice.trade_offs) >= 2
        trade_offs_text = " ".join(advice.trade_offs)
        assert "Speed vs Time" in trade_offs_text or "delivery" in trade_offs_text.lower()
        assert "Financial Cost" in trade_offs_text or "$" in trade_offs_text

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 8: Rejects invalid LLM recommendations (Unknown provider hallucination)
# -----------------------------------------------------------------------------
def test_rejects_invalid_llm_recommendations():
    async def run():
        request = create_sample_request(quantity=3)
        valid_offer = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=120.0)

        # LLM hallucinates non-existent provider 'phantom-clinic'
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "recommended_action": "RECOMMEND_ACCEPTANCE",
                    "recommended_provider_id": "phantom-clinic",
                    "counteroffer_quantity": None,
                    "evaluations": [],
                    "trade_offs": [],
                    "explanation": "Phantom clinic is ideal.",
                })
            ]
        )
        advisor = LLMNegotiationAdvisor(llm_client=LLMClient(provider=mock_provider))
        advice = await advisor.advise_async(request, [valid_offer])

        # Hallucination detected; fallback resolves to actual valid provider
        assert advice.recommended_provider_id == "lab-c"
        assert advice.recommended_action == "RECOMMEND_ACCEPTANCE"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 9: Falls back when the LLM is unavailable
# -----------------------------------------------------------------------------
def test_falls_back_when_llm_is_unavailable():
    async def run():
        request = create_sample_request(quantity=3)
        offer = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=100.0)

        # Provider times out
        mock_provider = MockLLMProvider(should_timeout=True)
        advisor = LLMNegotiationAdvisor(
            llm_client=LLMClient(provider=mock_provider, timeout=0.1, max_retries=0),
            enable_fallback=True,
        )

        advice = await advisor.advise_async(request, [offer])

        # Seamless deterministic fallback
        assert advice.recommended_action == "RECOMMEND_ACCEPTANCE"
        assert advice.recommended_provider_id == "lab-c"
        assert "Recommend accepting offer from Lab C" in advice.explanation

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 10: Never automatically accepts an offer
# -----------------------------------------------------------------------------
def test_never_automatically_accepts_an_offer():
    async def run():
        request = create_sample_request(quantity=3)
        offer = create_sample_offer("lab-c", "Lab C", quantity=3, delivery_hours=2, cost_amount=100.0)

        advisor = LLMNegotiationAdvisor()
        advice = await advisor.advise_async(request, [offer])

        # Architectural invariant: advice provides a recommendation, not an on-chain commitment
        assert advice.recommended_action in ("RECOMMEND_ACCEPTANCE", "SUGGEST_COUNTEROFFER", "REJECT_ALL")

        # When integrated with NegotiationAgent, proposal status remains PENDING/COUNTERED and agent status WAITING_FOR_APPROVAL
        agent = NegotiationAgent(advisor=advisor)
        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Workflow",
            receiver="NegotiationAgent",
            message_type="NEGOTIATE_OFFERS",
            payload={
                "resource_request": request.model_dump(),
                "offers": [offer.model_dump()],
            },
        )

        result = await agent.execute(msg)
        assert result.data["proposal"]["status"] == NegotiationStatus.PENDING.value
        assert agent.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["negotiation_advice"] is not None

    asyncio.run(run())
