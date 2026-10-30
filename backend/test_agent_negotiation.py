import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
import pytest

from app.agents.negotiation_agent import NegotiationAgent
from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from app.schemas.negotiation import NegotiationProposal, NegotiationStatus
from app.schemas.provider_offer import OfferStatus, ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)


def make_request(
    quantity: int = 4,
    required_hours: int = 12,
    max_budget: Optional[float] = 500.0,
    preferred: Optional[list[str]] = None,
) -> ResourceRequest:
    now = datetime.now(timezone.utc)
    specs = {"max_budget": max_budget} if max_budget is not None else {}
    return ResourceRequest(
        requester_institution_id="hospital-a",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=quantity,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city="Metropolis"),
        required_by=now + timedelta(hours=required_hours),
        required_by_hours=required_hours,
        constraints=ResourceConstraints(
            preferred_providers=preferred or [],
            specifications=specs,
        ),
    )


def make_offer(
    provider_id: str = "lab-c",
    offered_quantity: int = 4,
    delivery_hours: int = 2,
    cost_amount: float = 120.0,
    is_expired: bool = False,
    city: str = "Metropolis",
    resource_type: ResourceType = ResourceType.DIAGNOSTIC_SLOT,
) -> ProviderOffer:
    now = datetime.now(timezone.utc)
    if is_expired:
        created = now - timedelta(hours=5)
        expiry = now - timedelta(hours=1)
    else:
        created = now
        expiry = now + timedelta(hours=8)
    return ProviderOffer(
        request_id=uuid4(),
        provider_id=provider_id,
        provider_name=f"Provider {provider_id}",
        resource_type=resource_type,
        offered_quantity=offered_quantity,
        location=LocationArea(city=city),
        available_from=created,
        available_until=created + timedelta(days=2),
        created_at=created,
        expires_at=expiry,
        estimated_delivery_hours=delivery_hours,
        cost=ProviderCost(amount=cost_amount, currency="USD"),
        status=OfferStatus.OFFERED,
    )


def make_negotiation_message(
    request: ResourceRequest,
    offers: list[ProviderOffer],
    history: Optional[list[NegotiationProposal]] = None,
) -> AgentMessage:
    return AgentMessage(
        workflow_id=uuid4(),
        sender="Orchestrator",
        receiver="NegotiationAgent",
        message_type="NEGOTIATE_OFFERS",
        payload={
            "resource_request": request.model_dump(),
            "offers": [o.model_dump() for o in offers],
            "history": [h.model_dump() for h in (history or [])],
        },
    )


def test_selects_best_matching_offer():
    """Verify selecting the best full matching offer based on preferred status and delivery SLA."""
    async def run():
        agent = NegotiationAgent()
        req = make_request(quantity=4, preferred=["hospital-b"])

        # lab-c has shorter delivery (2h vs 4h), but hospital-b is preferred
        offer_c = make_offer("lab-c", offered_quantity=4, delivery_hours=2, cost_amount=150.0)
        offer_b = make_offer("hospital-b", offered_quantity=4, delivery_hours=4, cost_amount=140.0)

        msg = make_negotiation_message(req, [offer_c, offer_b])
        result = await agent.execute(msg)

        assert isinstance(result, AgentResult)
        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["success"] is True
        assert result.data["is_counteroffer"] is False
        assert result.data["selected_offer"]["provider_id"] == "hospital-b"

        proposal = NegotiationProposal.model_validate(result.data["proposal"])
        assert proposal.status == NegotiationStatus.PENDING
        assert proposal.proposed_quantity == 4
        assert proposal.round_number == 1

    asyncio.run(run())


def test_rejects_incompatible_offers():
    """Verify rejection of offers with mismatched resource types or incompatible locations."""
    async def run():
        agent = NegotiationAgent()
        req = make_request(quantity=2)

        # 1. Wrong resource type
        wrong_res = make_offer("lab-x", resource_type=ResourceType.BLOOD_UNIT)
        # 2. Incompatible city
        wrong_city = make_offer("lab-y", city="Gotham")

        msg = make_negotiation_message(req, [wrong_res, wrong_city])
        result = await agent.execute(msg)

        assert result.data["proposal"] is None
        assert len(result.data["rejections"]) == 2
        assert "Resource type mismatch" in result.data["rejections"][0]["reason"]
        assert "Location/service area incompatible" in result.data["rejections"][1]["reason"]

    asyncio.run(run())


def test_handles_partial_quantity_and_creates_counteroffer():
    """Verify creating a counteroffer when only partial quantity is available."""
    async def run():
        agent = NegotiationAgent(allow_partial_quantity=True, min_partial_ratio=0.5)
        req = make_request(quantity=4)  # Requesting 4 slots

        # Provider only offers 2 slots (50% partial)
        partial_offer = make_offer("lab-partial", offered_quantity=2, delivery_hours=2)

        msg = make_negotiation_message(req, [partial_offer])
        result = await agent.execute(msg)

        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["is_counteroffer"] is True

        proposal = NegotiationProposal.model_validate(result.data["proposal"])
        assert proposal.status == NegotiationStatus.COUNTERED
        assert proposal.proposed_quantity == 2
        assert "partial quantity (2/4)" in proposal.rationale

    asyncio.run(run())


def test_rejects_offers_exceeding_budget():
    """Verify offers with cost greater than max_budget are rejected."""
    async def run():
        agent = NegotiationAgent()
        req = make_request(quantity=2, max_budget=200.0)

        expensive_offer = make_offer("lab-expensive", cost_amount=350.0)

        msg = make_negotiation_message(req, [expensive_offer])
        result = await agent.execute(msg)

        assert result.data["proposal"] is None
        assert len(result.data["rejections"]) == 1
        assert "exceeds budget" in result.data["rejections"][0]["reason"]

    asyncio.run(run())


def test_rejects_offers_outside_deadline():
    """Verify offers with delivery SLA exceeding required_by_hours are rejected."""
    async def run():
        agent = NegotiationAgent()
        req = make_request(quantity=2, required_hours=6)

        slow_offer = make_offer("lab-slow", delivery_hours=12)

        msg = make_negotiation_message(req, [slow_offer])
        result = await agent.execute(msg)

        assert result.data["proposal"] is None
        assert len(result.data["rejections"]) == 1
        assert "exceeds required deadline" in result.data["rejections"][0]["reason"]

    asyncio.run(run())


def test_preserves_negotiation_history_across_rounds():
    """Verify history lineage: round_number increments and previous_proposal_id is linked."""
    async def run():
        agent = NegotiationAgent()
        req = make_request(quantity=4)
        offer = make_offer("lab-c", offered_quantity=4)

        # Round 1
        r1_proposal = NegotiationProposal(
            workflow_id=uuid4(),
            request_id=req.request_id,
            provider_offer_id=offer.offer_id,
            proposed_quantity=4,
            proposed_by="hospital-a",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            status=NegotiationStatus.PENDING,
            round_number=1,
        )

        msg = make_negotiation_message(req, [offer], history=[r1_proposal])
        result = await agent.execute(msg)

        proposal2 = NegotiationProposal.model_validate(result.data["proposal"])
        assert proposal2.round_number == 2
        assert proposal2.previous_proposal_id == r1_proposal.negotiation_id

        history = result.data["negotiation_history"]
        assert len(history) == 2
        assert str(history[0]["negotiation_id"]) == str(r1_proposal.negotiation_id)
        assert str(history[1]["negotiation_id"]) == str(proposal2.negotiation_id)

    asyncio.run(run())


def test_handles_expired_offers():
    """Verify expired offers are rejected immediately."""
    async def run():
        agent = NegotiationAgent()
        req = make_request(quantity=2)
        expired_offer = make_offer("lab-c", is_expired=True)

        msg = make_negotiation_message(req, [expired_offer])
        result = await agent.execute(msg)

        assert result.data["proposal"] is None
        assert len(result.data["rejections"]) == 1
        assert "Offer has expired" in result.data["rejections"][0]["reason"]

    asyncio.run(run())


def test_does_not_automatically_accept_or_reserve_capacity():
    """Governance Invariant: Agent must never auto-accept or claim reservation authority."""
    async def run():
        agent = NegotiationAgent()
        req = make_request(quantity=2)
        perfect_offer = make_offer("lab-c", offered_quantity=2, delivery_hours=1, cost_amount=100.0)

        msg = make_negotiation_message(req, [perfect_offer])
        result = await agent.execute(msg)

        # Invariant checks:
        # 1. Agent status is WAITING_FOR_APPROVAL (never COMPLETED with auto-reservation)
        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert agent.status == AgentStatus.WAITING_FOR_APPROVAL

        # 2. Proposal status is PENDING (never ACCEPTED automatically)
        proposal = NegotiationProposal.model_validate(result.data["proposal"])
        assert proposal.status == NegotiationStatus.PENDING
        assert "Awaiting institutional authorization" in proposal.rationale

    asyncio.run(run())
