import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from .base import BaseAgent
from ..schemas.agent_messages import AgentMessage, AgentStatus
from ..schemas.negotiation import NegotiationProposal, NegotiationStatus
from ..schemas.provider_offer import OfferStatus, ProviderCost, ProviderOffer
from ..schemas.resource_request import ResourceRequest, ResourceType

logger = logging.getLogger("medway.agents.negotiation")


class NegotiationAgent(BaseAgent):
    """
    Autonomous Negotiation Agent.
    
    Evaluates candidate provider offers against request requirements, institutional budget,
    and deadlines. Constructs structured negotiation proposals and counteroffers.
    
    Architectural & Governance Invariants:
    1. Deterministic Policy Enforcement: Hard constraints (quantity, budget, deadline)
       are strictly enforced by policy code, not left to probabilistic LLM heuristics.
    2. Zero Automatic Commitment: NEVER automatically commits reservations or claims
       on-chain capacity. A selected offer transitions to WAITING_FOR_APPROVAL.
    3. Counteroffer Immutability: Counteroffers generate new proposal records incrementing
       round_number and linking previous_proposal_id without overwriting original offers.
    4. Lineage Tracking: Preserves negotiation history across all proposal rounds.
    """

    def __init__(
        self,
        name: str = "NegotiationAgent",
        default_max_budget: Optional[float] = None,
        allow_partial_quantity: bool = True,
        min_partial_ratio: float = 0.5,
    ) -> None:
        super().__init__(name=name)
        self.default_max_budget = default_max_budget
        self.allow_partial_quantity = allow_partial_quantity
        self.min_partial_ratio = min_partial_ratio

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Evaluate candidate offers against request criteria and construct negotiation proposals.
        
        Expected payload:
            - resource_request (dict): Validated ResourceRequest.
            - offers (list[dict]): Candidate ProviderOffer objects.
            - history (list[dict], optional): Previous NegotiationProposal objects.
            - max_budget (float, optional): Maximum acceptable cost constraint.
        """
        payload = message.payload or {}
        req_raw = payload.get("resource_request") or payload.get("request")
        if not req_raw:
            raise ValueError("NegotiationAgent requires 'resource_request' in message payload")

        request = ResourceRequest.model_validate(req_raw)

        raw_offers = payload.get("offers") or payload.get("candidate_offers") or []
        offers = [ProviderOffer.model_validate(o) for o in raw_offers]

        # Extract history
        raw_history = payload.get("history") or payload.get("negotiation_history") or []
        history = [NegotiationProposal.model_validate(h) for h in raw_history]

        # Extract budget constraints
        max_budget = payload.get("max_budget", self.default_max_budget)
        if max_budget is None and request.constraints and request.constraints.specifications:
            max_budget = request.constraints.specifications.get("max_cost") or request.constraints.specifications.get("max_budget")

        now = datetime.now(timezone.utc)
        rejections: list[dict[str, Any]] = []
        eligible_full: list[tuple[ProviderOffer, dict[str, Any]]] = []
        eligible_partial: list[tuple[ProviderOffer, dict[str, Any]]] = []

        preferred_set = set(request.constraints.preferred_providers) if request.constraints else set()

        # 1. Compare and Filter Candidate Offers
        for offer in offers:
            p_id = offer.provider_id

            # Rule 1: Check Offer Expiry
            if offer.expires_at <= now:
                rejections.append({
                    "provider_id": p_id,
                    "offer_id": str(offer.offer_id),
                    "reason": "Offer has expired",
                })
                continue

            # Rule 2: Resource Type Compatibility
            if offer.resource_type != request.resource_type:
                rejections.append({
                    "provider_id": p_id,
                    "offer_id": str(offer.offer_id),
                    "reason": f"Resource type mismatch: offered {offer.resource_type}, requested {request.resource_type}",
                })
                continue

            # Rule 3: Deadline Compliance
            if request.required_by_hours and offer.estimated_delivery_hours:
                if offer.estimated_delivery_hours > request.required_by_hours:
                    rejections.append({
                        "provider_id": p_id,
                        "offer_id": str(offer.offer_id),
                        "reason": (
                            f"Delivery time ({offer.estimated_delivery_hours}h) exceeds "
                            f"required deadline ({request.required_by_hours}h)"
                        ),
                    })
                    continue

            # Rule 4: Budget Compliance
            if max_budget is not None and offer.cost is not None:
                if offer.cost.amount > max_budget:
                    rejections.append({
                        "provider_id": p_id,
                        "offer_id": str(offer.offer_id),
                        "reason": f"Cost ({offer.cost.amount} {offer.cost.currency}) exceeds budget ({max_budget})",
                    })
                    continue

            # Rule 5: Location Compatibility
            if request.location and offer.location:
                if not self._is_location_compatible(request.location, offer.location):
                    rejections.append({
                        "provider_id": p_id,
                        "offer_id": str(offer.offer_id),
                        "reason": "Location/service area incompatible with request",
                    })
                    continue

            # Rule 6: Quantity Evaluation (Full vs Partial)
            meta = {
                "delivery_hours": offer.estimated_delivery_hours or 24,
                "cost_amount": offer.cost.amount if offer.cost else 0.0,
                "is_preferred": p_id in preferred_set,
            }

            if offer.offered_quantity >= request.quantity:
                eligible_full.append((offer, meta))
            elif self.allow_partial_quantity and offer.offered_quantity >= int(request.quantity * self.min_partial_ratio):
                eligible_partial.append((offer, meta))
            else:
                rejections.append({
                    "provider_id": p_id,
                    "offer_id": str(offer.offer_id),
                    "reason": f"Insufficient quantity: offered {offer.offered_quantity} < requested {request.quantity}",
                })

        # 2. Handle Zero Eligible Offers
        if not eligible_full and not eligible_partial:
            return {
                "success": True,
                "proposal": None,
                "selected_offer": None,
                "is_counteroffer": False,
                "rejections": rejections,
                "negotiation_history": [h.model_dump() for h in history],
                "message": "No provider offers met the requested constraints and policy limits.",
            }

        # 3. Deterministic Ranking
        def rank_key(item: tuple[ProviderOffer, dict[str, Any]]):
            offer, meta = item
            return (
                0 if meta["is_preferred"] else 1,
                meta["delivery_hours"],
                meta["cost_amount"],
            )

        eligible_full.sort(key=rank_key)
        eligible_partial.sort(key=rank_key)

        # 4. Construct Negotiation Proposal
        expiry_deadline = now + timedelta(hours=4)
        round_num = len(history) + 1
        prev_id = history[-1].negotiation_id if history else None

        # Prioritize full fulfillment; fallback to partial counteroffer
        if eligible_full:
            selected_offer, meta = eligible_full[0]
            proposal = NegotiationProposal(
                workflow_id=message.workflow_id,
                request_id=request.request_id,
                provider_offer_id=selected_offer.offer_id,
                previous_proposal_id=prev_id,
                round_number=round_num,
                proposed_quantity=request.quantity,
                proposed_cost=selected_offer.cost,
                proposed_conditions=selected_offer.conditions,
                proposed_by=request.requester_institution_id,
                counterparty_id=selected_offer.provider_id,
                created_at=now,
                expires_at=expiry_deadline,
                status=NegotiationStatus.PENDING,
                rationale=(
                    f"Selected full-capacity offer from {selected_offer.provider_name} "
                    f"({selected_offer.estimated_delivery_hours}h SLA, {meta['cost_amount']} cost). "
                    "Awaiting institutional authorization."
                ),
            )
            is_counteroffer = False

        else:
            selected_offer, meta = eligible_partial[0]
            proposal = NegotiationProposal(
                workflow_id=message.workflow_id,
                request_id=request.request_id,
                provider_offer_id=selected_offer.offer_id,
                previous_proposal_id=prev_id,
                round_number=round_num,
                proposed_quantity=selected_offer.offered_quantity,
                proposed_cost=selected_offer.cost,
                proposed_conditions=selected_offer.conditions,
                proposed_by=request.requester_institution_id,
                counterparty_id=selected_offer.provider_id,
                created_at=now,
                expires_at=expiry_deadline,
                status=NegotiationStatus.COUNTERED,
                rationale=(
                    f"Generated counteroffer for partial quantity ({selected_offer.offered_quantity}/{request.quantity}) "
                    f"from {selected_offer.provider_name}. Requires institutional authorization."
                ),
            )
            is_counteroffer = True

        # Invariant: Never automatically commit or reserve capacity
        # Transitions agent status to WAITING_FOR_APPROVAL
        self.set_status(AgentStatus.WAITING_FOR_APPROVAL)

        updated_history = [*history, proposal]

        return {
            "success": True,
            "proposal": proposal.model_dump(),
            "selected_offer": selected_offer.model_dump(),
            "is_counteroffer": is_counteroffer,
            "rejections": rejections,
            "negotiation_history": [h.model_dump() for h in updated_history],
            "message": (
                f"Generated {'counteroffer' if is_counteroffer else 'negotiation proposal'} "
                f"for provider {selected_offer.provider_id}. Authorization required."
            ),
        }

    def _is_location_compatible(self, req_loc: Any, offer_loc: Any) -> bool:
        """Verify geographic compatibility between request and offer."""
        r_city = getattr(req_loc, "city", None)
        o_city = getattr(offer_loc, "city", None)
        r_postal = getattr(req_loc, "postal_code", None)
        o_postal = getattr(offer_loc, "postal_code", None)

        if r_city and o_city and r_city.lower() not in o_city.lower() and o_city.lower() not in r_city.lower():
            return False

        if r_postal and o_postal and r_postal.lower() != o_postal.lower():
            return False

        return True

