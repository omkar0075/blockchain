import asyncio
import concurrent.futures
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..schemas.negotiation import NegotiationProposal
from ..schemas.provider_offer import ProviderOffer
from ..schemas.resource_request import ResourceRequest
from .client import LLMClient
from .guardrails import LLMGuardrailViolation, LLMOutputValidationError
from .schemas import (
    LLMError,
    NegotiationAdviceOutput,
    OfferEvaluation,
)

logger = logging.getLogger("medway.llm.negotiation")

NEGOTIATION_ADVISOR_SYSTEM_PROMPT = """You are MEDway's Healthcare Resource Negotiation Advisor.
Your role is to analyze candidate healthcare provider offers against the institutional request, identify trade-offs, and recommend the best strategy.

Governance & Policy Invariants:
1. Strict Constraints: NEVER recommend an offer that exceeds the stated maximum budget or delivery deadline.
2. Partial Quantities: If an offer has partial quantity, recommend 'SUGGEST_COUNTEROFFER' with counteroffer_quantity equal to the offered capacity (never exceed requested quantity).
3. Trade-offs: Clearly articulate trade-offs across speed (turnaround hours), financial cost, volume, and location proximity.
4. Advisory Role Only: You explain and suggest; you NEVER claim authority to accept offers, lock capacity, or sign blockchain transactions.
5. Incompatible Offers: If all offers exceed budget or miss deadlines, recommend 'REJECT_ALL' and set recommended_provider_id to null.

Output must strictly conform to NegotiationAdviceOutput."""


class LLMNegotiationAdvisor:
    """
    LLM-Powered Healthcare Negotiation Advisor.

    Provides deep reasoning, comparative evaluation, and counteroffer suggestions
    for candidate provider offers while respecting strict deterministic constraints.

    Architectural Invariants:
    1. Advisory Boundary: Recommends and explains, but NEVER accepts offers or commits capacity.
    2. Constraint Verification: Budget, deadline, and quantity constraints are strictly verified
       by deterministic checks; hallucinations or constraint-violating recommendations are rejected.
    3. Deterministic Fallback: When the model is unavailable or returns invalid guidance,
       falls back immediately to rule-based ranking and explanations.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        enable_fallback: bool = True,
    ) -> None:
        self.client = llm_client or LLMClient()
        self.enable_fallback = enable_fallback

    async def advise_async(
        self,
        request: ResourceRequest,
        offers: list[ProviderOffer],
        max_budget: Optional[float] = None,
        history: Optional[list[NegotiationProposal]] = None,
    ) -> NegotiationAdviceOutput:
        """
        Asynchronously evaluate provider offers using the LLM with deterministic safety enforcement.
        """
        if not offers:
            return NegotiationAdviceOutput(
                recommended_action="REJECT_ALL",
                recommended_provider_id=None,
                counteroffer_quantity=None,
                evaluations=[],
                trade_offs=["Zero candidate offers received from providers."],
                explanation="No provider offers were available to evaluate.",
            )

        # Budget extraction fallback
        effective_budget = max_budget
        if effective_budget is None and request.constraints and request.constraints.specifications:
            effective_budget = (
                request.constraints.specifications.get("max_cost")
                or request.constraints.specifications.get("max_budget")
            )

        # Build structured evaluation prompt
        prompt = self._build_evaluation_prompt(request, offers, effective_budget, history)

        try:
            raw_advice: NegotiationAdviceOutput = await self.client.generate_structured(
                prompt=prompt,
                schema=NegotiationAdviceOutput,
                system_prompt=NEGOTIATION_ADVISOR_SYSTEM_PROMPT,
            )

            # Deterministic post-validation: verify LLM recommendation does not violate hard limits
            validated_advice = self._validate_and_sanitize_recommendation(
                advice=raw_advice,
                request=request,
                offers=offers,
                max_budget=effective_budget,
            )
            return validated_advice

        except (LLMError, LLMOutputValidationError, LLMGuardrailViolation) as exc:
            if not self.enable_fallback:
                raise
            logger.warning("LLM negotiation advisor unavailable (%s); using deterministic evaluation", exc)
            return self._evaluate_offers_deterministically(request, offers, effective_budget)
        except Exception as exc:
            if not self.enable_fallback:
                raise
            logger.error("Unexpected error in LLM negotiation advisor (%s); using fallback", exc)
            return self._evaluate_offers_deterministically(request, offers, effective_budget)

    def advise(
        self,
        request: ResourceRequest,
        offers: list[ProviderOffer],
        max_budget: Optional[float] = None,
        history: Optional[list[NegotiationProposal]] = None,
    ) -> NegotiationAdviceOutput:
        """
        Synchronous wrapper for advise_async.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run,
                    self.advise_async(request, offers, max_budget, history),
                ).result(timeout=15.0)
        else:
            return asyncio.run(
                self.advise_async(request, offers, max_budget, history)
            )

    def _build_evaluation_prompt(
        self,
        request: ResourceRequest,
        offers: list[ProviderOffer],
        max_budget: Optional[float],
        history: Optional[list[NegotiationProposal]],
    ) -> str:
        """Construct a structured representation of the coordination requirements and candidate offers."""
        lines = [
            f"Requester Institution: {request.requester_institution_id}",
            f"Requested Resource: {request.resource_type.value if hasattr(request.resource_type, 'value') else str(request.resource_type)}",
            f"Requested Quantity: {request.quantity}",
            f"Urgency: {request.urgency.value if hasattr(request.urgency, 'value') else str(request.urgency)}",
            f"Delivery Deadline: {request.required_by_hours} hours" if request.required_by_hours else "Delivery Deadline: Unspecified",
            f"Maximum Budget: {max_budget} USD" if max_budget else "Maximum Budget: Unconstrained",
            f"Target Location: {request.location.city if request.location else 'Flexible'}",
            "",
            f"Candidate Offers ({len(offers)}):",
        ]

        for i, o in enumerate(offers, 1):
            cost_str = f"{o.cost.amount} {o.cost.currency}" if o.cost else "Unspecified"
            delivery_str = f"{o.estimated_delivery_hours}h" if o.estimated_delivery_hours else "Unspecified"
            city_str = o.location.city if o.location else "Unspecified"
            lines.append(
                f"{i}. Provider ID: '{o.provider_id}' ({o.provider_name})\n"
                f"   - Quantity: {o.offered_quantity}\n"
                f"   - Delivery Time: {delivery_str}\n"
                f"   - Total Cost: {cost_str}\n"
                f"   - Facility Location: {city_str}\n"
                f"   - Expiry: {o.expires_at.isoformat()}"
            )

        if history:
            lines.append(f"\nPrior Negotiation Rounds: {len(history)} round(s) recorded.")

        return "\n".join(lines)

    def _validate_and_sanitize_recommendation(
        self,
        advice: NegotiationAdviceOutput,
        request: ResourceRequest,
        offers: list[ProviderOffer],
        max_budget: Optional[float],
    ) -> NegotiationAdviceOutput:
        """
        Verify that the LLM's recommendation strictly complies with deterministic constraints.
        If the LLM recommends an offer violating budget, deadline, or quantity, override with fallback.
        """
        offers_by_id = {o.provider_id: o for o in offers}
        now = datetime.now(timezone.utc)

        # 1. Check if recommended provider exists
        rec_id = advice.recommended_provider_id
        if rec_id:
            if rec_id not in offers_by_id:
                logger.warning("LLM hallucinated non-existent provider '%s'; overriding with fallback", rec_id)
                return self._evaluate_offers_deterministically(request, offers, max_budget)

            rec_offer = offers_by_id[rec_id]

            # 2. Check offer expiry
            if rec_offer.expires_at <= now:
                logger.warning("LLM recommended expired offer '%s'; overriding with fallback", rec_id)
                return self._evaluate_offers_deterministically(request, offers, max_budget)

            # 3. Check budget constraint
            if max_budget is not None and rec_offer.cost and rec_offer.cost.amount > max_budget:
                logger.warning("LLM recommended offer '%s' exceeding budget ($%s > $%s); overriding", rec_id, rec_offer.cost.amount, max_budget)
                return self._evaluate_offers_deterministically(request, offers, max_budget)

            # 4. Check deadline constraint
            if request.required_by_hours and rec_offer.estimated_delivery_hours:
                if rec_offer.estimated_delivery_hours > request.required_by_hours:
                    logger.warning("LLM recommended offer '%s' exceeding deadline (%sh > %sh); overriding", rec_id, rec_offer.estimated_delivery_hours, request.required_by_hours)
                    return self._evaluate_offers_deterministically(request, offers, max_budget)

            # 5. Check counteroffer quantity
            if advice.recommended_action == "SUGGEST_COUNTEROFFER":
                if advice.counteroffer_quantity is None or advice.counteroffer_quantity > request.quantity:
                    advice.counteroffer_quantity = min(rec_offer.offered_quantity, request.quantity)

            # 6. Ensure full quantity offers are not mislabeled as counteroffers
            elif rec_offer.offered_quantity < request.quantity and advice.recommended_action != "SUGGEST_COUNTEROFFER":
                advice.recommended_action = "SUGGEST_COUNTEROFFER"
                advice.counteroffer_quantity = rec_offer.offered_quantity

        elif advice.recommended_action != "REJECT_ALL":
            # Inconsistent: no provider recommended but action is not REJECT_ALL
            return self._evaluate_offers_deterministically(request, offers, max_budget)

        return advice

    def _evaluate_offers_deterministically(
        self,
        request: ResourceRequest,
        offers: list[ProviderOffer],
        max_budget: Optional[float] = None,
    ) -> NegotiationAdviceOutput:
        """
        Rule-based deterministic evaluation engine providing fallback advice and trade-off analysis.
        """
        now = datetime.now(timezone.utc)
        evaluations: list[OfferEvaluation] = []
        eligible_full: list[tuple[ProviderOffer, float, float]] = []
        eligible_partial: list[tuple[ProviderOffer, float, float]] = []
        trade_offs: list[str] = []

        preferred_providers = set(request.constraints.preferred_providers) if request.constraints else set()

        for offer in offers:
            strengths: list[str] = []
            weaknesses: list[str] = []
            meets_hard = True

            # Expiry
            if offer.expires_at <= now:
                meets_hard = False
                weaknesses.append("Offer has already expired.")

            # Resource Type
            if offer.resource_type != request.resource_type:
                meets_hard = False
                weaknesses.append(f"Resource type mismatch: offered {offer.resource_type}, requested {request.resource_type}")

            # Deadline
            delivery_h = offer.estimated_delivery_hours or 24.0
            if request.required_by_hours and delivery_h > request.required_by_hours:
                meets_hard = False
                weaknesses.append(f"Delivery window ({delivery_h}h) exceeds deadline ({request.required_by_hours}h)")
            else:
                strengths.append(f"Fast delivery turnaround: {delivery_h} hours.")

            # Budget
            cost_amount = offer.cost.amount if offer.cost else 0.0
            if max_budget is not None and offer.cost and offer.cost.amount > max_budget:
                meets_hard = False
                weaknesses.append(f"Cost (${cost_amount}) exceeds maximum budget (${max_budget})")
            elif offer.cost:
                strengths.append(f"Within budget: ${cost_amount} {offer.cost.currency}")

            # Quantity
            if offer.offered_quantity >= request.quantity:
                strengths.append(f"Full volume capacity: offers {offer.offered_quantity} (requested {request.quantity})")
                if meets_hard:
                    eligible_full.append((offer, delivery_h, cost_amount))
            else:
                weaknesses.append(f"Partial volume: offers {offer.offered_quantity} of {request.quantity} requested units")
                if meets_hard and offer.offered_quantity > 0:
                    eligible_partial.append((offer, delivery_h, cost_amount))

            evaluations.append(
                OfferEvaluation(
                    provider_id=offer.provider_id,
                    strengths=strengths,
                    weaknesses=weaknesses,
                    meets_hard_constraints=meets_hard,
                )
            )

        # Build trade-off notes
        if len(offers) >= 2:
            sorted_by_speed = sorted(offers, key=lambda x: x.estimated_delivery_hours or 999)
            fastest = sorted_by_speed[0]
            slowest = sorted_by_speed[-1]
            if fastest.provider_id != slowest.provider_id:
                trade_offs.append(
                    f"Speed vs Time: {fastest.provider_name} delivers in {fastest.estimated_delivery_hours}h "
                    f"compared to {slowest.provider_name} ({slowest.estimated_delivery_hours}h)."
                )

            costed_offers = [o for o in offers if o.cost]
            if len(costed_offers) >= 2:
                sorted_by_cost = sorted(costed_offers, key=lambda x: x.cost.amount)  # type: ignore
                cheapest = sorted_by_cost[0]
                priciest = sorted_by_cost[-1]
                if cheapest.provider_id != priciest.provider_id:
                    trade_offs.append(
                        f"Financial Cost: {cheapest.provider_name} is ${cheapest.cost.amount} "  # type: ignore
                        f"versus ${priciest.cost.amount} from {priciest.provider_name}."  # type: ignore
                    )

        # Deterministic Ranking Function: Preferred -> Speed -> Cost
        def rank_tuple(item: tuple[ProviderOffer, float, float]):
            off, speed, cost = item
            is_pref = 0 if off.provider_id in preferred_providers else 1
            return (is_pref, speed, cost)

        eligible_full.sort(key=rank_tuple)
        eligible_partial.sort(key=rank_tuple)

        if eligible_full:
            winner, win_speed, win_cost = eligible_full[0]
            return NegotiationAdviceOutput(
                recommended_action="RECOMMEND_ACCEPTANCE",
                recommended_provider_id=winner.provider_id,
                counteroffer_quantity=None,
                evaluations=evaluations,
                trade_offs=trade_offs,
                explanation=(
                    f"Recommend accepting offer from {winner.provider_name} ({winner.provider_id}). "
                    f"Provides full capacity of {winner.offered_quantity} units within {win_speed}h "
                    f"at cost of ${win_cost} USD."
                ),
            )

        if eligible_partial:
            winner, win_speed, win_cost = eligible_partial[0]
            counter_qty = min(winner.offered_quantity, request.quantity)
            return NegotiationAdviceOutput(
                recommended_action="SUGGEST_COUNTEROFFER",
                recommended_provider_id=winner.provider_id,
                counteroffer_quantity=counter_qty,
                evaluations=evaluations,
                trade_offs=trade_offs,
                explanation=(
                    f"No provider offers full capacity. Suggest counteroffer for {counter_qty} units "
                    f"to {winner.provider_name} ({winner.provider_id}) with {win_speed}h delivery."
                ),
            )

        return NegotiationAdviceOutput(
            recommended_action="REJECT_ALL",
            recommended_provider_id=None,
            counteroffer_quantity=None,
            evaluations=evaluations,
            trade_offs=trade_offs,
            explanation=(
                "All candidate provider offers violate strict constraints (budget, deadline, or resource mismatch). "
                "Reject all offers and seek alternative discovery."
            ),
        )

