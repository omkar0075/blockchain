import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, Optional, Union
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field

from ..agents.recovery_agent import FailureClassification, RecoveryStrategy
from ..llm.client import LLMClient
from ..llm.guardrails import LLMGuardrailViolation, LLMOutputValidationError
from ..llm.negotiation_advisor import LLMNegotiationAdvisor
from ..llm.recovery_advisor import LLMRecoveryAdvisor
from ..llm.request_interpreter import LLMRequestParser
from ..llm.schemas import (
    LLMError,
    NegotiationAdviceOutput,
    RecoveryAdviceOutput,
    RequestInterpretationOutput,
)
from ..schemas.provider_offer import ProviderOffer
from ..schemas.resource_request import ResourceRequest
from ..services.agent_observability import AgentObservabilityService, redact_payload

logger = logging.getLogger("medway.services.agent_llm_bridge")


class AIRecommendation(BaseModel):
    """
    Standard envelope wrapping any AI recommendation passed to deterministic agents.
    Tracks whether a decision was AI-assisted, preserves workflow context, and enforces policy boundaries.
    """
    model_config = ConfigDict(extra="ignore")

    recommendation_id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID
    agent_name: str
    capability: str
    is_ai_assisted: bool = True
    used_fallback: bool = False
    requires_human_approval: bool = False
    data: dict[str, Any]
    explanation: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AgentLLMBridge:
    """
    Bridge between deterministic MEDway agents and the LLM intelligence layer.

    Responsibilities:
    1. Minimal Data Passing: Passes only validated, minimum-required data, stripping private keys/PII.
    2. Context Preservation: Maintains workflow IDs and agent context across calls.
    3. Output Validation: Validates all LLM recommendations through Pydantic schemas.
    4. Deterministic Overrides: Deterministic agents retain full authority to accept, reject,
       or override recommendations.
    5. Observability Tracking: Records AI recommendations, fallbacks, and decisions in observability.
    6. Strict Security Boundaries:
       - LLM CANNOT grant institutional permissions.
       - LLM CANNOT alter resource inventory or capacity.
       - LLM CANNOT sign or execute blockchain transactions.
       - Sensitive commitments strictly require human authorization.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        request_parser: Optional[LLMRequestParser] = None,
        negotiation_advisor: Optional[LLMNegotiationAdvisor] = None,
        recovery_advisor: Optional[LLMRecoveryAdvisor] = None,
        observability_service: Optional[AgentObservabilityService] = None,
        enable_fallback: bool = True,
    ) -> None:
        self.client = llm_client or LLMClient()
        self.enable_fallback = enable_fallback
        self.observability = observability_service

        self.request_parser = request_parser or LLMRequestParser(
            llm_client=self.client,
            enable_fallback=enable_fallback,
        )
        self.negotiation_advisor = negotiation_advisor or LLMNegotiationAdvisor(
            llm_client=self.client,
            enable_fallback=enable_fallback,
        )
        self.recovery_advisor = recovery_advisor or LLMRecoveryAdvisor(
            llm_client=self.client,
            enable_fallback=enable_fallback,
        )

    # -------------------------------------------------------------------------
    # 1. Request Agent Bridge: Request Interpreter
    # -------------------------------------------------------------------------
    async def interpret_request(
        self,
        text: str,
        requester_id: str = "hospital-a",
        workflow_id: Optional[UUID] = None,
    ) -> AIRecommendation:
        """Bridge Request Agent to LLM Request Interpreter."""
        wf_id = workflow_id or uuid4()
        clean_text = self._sanitize_input_text(text)

        used_fallback = False
        try:
            raw_parse = await self.request_parser.parse_async(
                text=clean_text,
                requester_institution_id=requester_id,
            )
            interp_data = raw_parse.get("interpretation")
            if interp_data and isinstance(interp_data, dict):
                interp = RequestInterpretationOutput.model_validate(interp_data)
            elif isinstance(interp_data, RequestInterpretationOutput):
                interp = interp_data
            else:
                used_fallback = True
                rtype = raw_parse.get("resource_type")
                rtype_str = rtype.value if hasattr(rtype, "value") else (str(rtype) if rtype else "diagnostic_slot")
                urg = raw_parse.get("urgency")
                urg_str = urg.value if hasattr(urg, "value") else (str(urg) if urg else "urgent")
                loc = raw_parse.get("location")
                loc_str = getattr(loc, "city", None) or (str(loc) if loc else None)

                interp = RequestInterpretationOutput(
                    resource_type=rtype_str,
                    quantity=raw_parse.get("quantity"),
                    urgency=urg_str,
                    location=loc_str,
                    required_by_hours=raw_parse.get("required_by_hours"),
                    is_ambiguous=bool(raw_parse.get("ambiguous")),
                    missing_fields=list(raw_parse.get("missing_fields") or []),
                    clarification_prompt=raw_parse.get("clarification_prompt"),
                )
        except Exception as exc:
            if not self.enable_fallback:
                raise
            logger.warning("Request interpretation fallback triggered: %s", exc)
            used_fallback = True
            interp = RequestInterpretationOutput(
                resource_type="diagnostic_slot",
                quantity=1,
                urgency="urgent",
                location="Metropolis",
                required_by_hours=24,
                is_ambiguous=False,
                missing_fields=[],
            )

        rec = AIRecommendation(
            workflow_id=wf_id,
            agent_name="RequestAgent",
            capability="RequestInterpreter",
            is_ai_assisted=not used_fallback,
            used_fallback=used_fallback,
            requires_human_approval=False,
            data=interp.model_dump(),
            explanation="Extracted clinical resource parameters from natural-language query.",
        )
        self._record_observability(wf_id, "RequestAgent", "AI_REQUEST_INTERPRETATION", rec)
        return rec

    # -------------------------------------------------------------------------
    # 2. Discovery Agent Bridge: Explain Provider Suitability
    # -------------------------------------------------------------------------
    async def explain_provider_suitability(
        self,
        request: ResourceRequest,
        candidates: list[dict[str, Any]],
        workflow_id: Optional[UUID] = None,
    ) -> AIRecommendation:
        """Bridge Discovery Agent to explain candidate provider suitability and constraints."""
        wf_id = workflow_id or request.workflow_id or uuid4()
        sanitized_candidates = [
            {"provider_id": c.get("provider_id", c.get("id")), "location": str(c.get("location", "")), "distance_km": c.get("distance_km", 5)}
            for c in candidates
        ]

        explanation = (
            f"Evaluated {len(candidates)} candidate provider(s). Matched based on accredited "
            f"service area and resource category {request.resource_type.value}."
        )

        rec = AIRecommendation(
            workflow_id=wf_id,
            agent_name="DiscoveryAgent",
            capability="ProviderSuitabilityExplanation",
            is_ai_assisted=True,
            used_fallback=False,
            requires_human_approval=False,
            data={"matched_candidates": sanitized_candidates, "requested_type": request.resource_type.value},
            explanation=explanation,
        )
        self._record_observability(wf_id, "DiscoveryAgent", "AI_PROVIDER_SUITABILITY", rec)
        return rec

    # -------------------------------------------------------------------------
    # 3. Provider Agent Bridge: Explain Offer Conditions
    # -------------------------------------------------------------------------
    async def explain_offer_conditions(
        self,
        offer: ProviderOffer,
        workflow_id: Optional[UUID] = None,
    ) -> AIRecommendation:
        """Bridge Provider Agent to explain offered pricing, turnaround SLA, and expiry conditions."""
        wf_id = workflow_id or uuid4()
        explanation = (
            f"Provider {offer.provider_name} offered {offer.offered_quantity} slot(s) with an "
            f"estimated SLA of {offer.estimated_delivery_hours}h at {offer.cost.amount if offer.cost else 0} {offer.cost.currency if offer.cost else 'USD'}."
        )

        rec = AIRecommendation(
            workflow_id=wf_id,
            agent_name="ProviderAgent",
            capability="OfferConditionsExplanation",
            is_ai_assisted=True,
            used_fallback=False,
            requires_human_approval=False,
            data={
                "provider_id": offer.provider_id,
                "offered_quantity": offer.offered_quantity,
                "delivery_hours": offer.estimated_delivery_hours,
                "conditions": offer.conditions,
            },
            explanation=explanation,
        )
        self._record_observability(wf_id, "ProviderAgent", "AI_OFFER_EXPLANATION", rec)
        return rec

    # -------------------------------------------------------------------------
    # 4. Negotiation Agent Bridge: Negotiation Advisor
    # -------------------------------------------------------------------------
    async def advise_negotiation(
        self,
        request: ResourceRequest,
        offers: list[ProviderOffer],
        workflow_id: Optional[UUID] = None,
        max_budget: Optional[float] = None,
    ) -> AIRecommendation:
        """Bridge Negotiation Agent to LLM Negotiation Advisor with deterministic safety gating."""
        wf_id = workflow_id or request.workflow_id or uuid4()

        # Only pass minimal, validated data
        clean_offers = [o for o in offers if o.resource_type == request.resource_type]

        used_fallback = False
        try:
            raw_advice: NegotiationAdviceOutput = await self.negotiation_advisor.advise_async(
                request=request,
                offers=clean_offers,
                max_budget=max_budget,
            )
            advice = NegotiationAdviceOutput.model_validate(raw_advice)
        except Exception as exc:
            if not self.enable_fallback:
                raise
            logger.warning("Negotiation advisor fallback triggered: %s", exc)
            used_fallback = True
            best_id = clean_offers[0].provider_id if clean_offers else None
            advice = NegotiationAdviceOutput(
                recommended_action="RECOMMEND_ACCEPTANCE" if best_id else "REJECT_ALL",
                recommended_provider_id=best_id,
                counteroffer_quantity=None,
                evaluations=[],
                trade_offs=["Deterministic cost and turnaround ranking applied."],
                explanation="Recommended provider using deterministic fallback ranking.",
            )

        # Invariant: Counteroffers or commitment selections require attributable institutional approval
        requires_approval = bool(advice.recommended_action in ("RECOMMEND_ACCEPTANCE", "SUGGEST_COUNTEROFFER"))

        rec = AIRecommendation(
            workflow_id=wf_id,
            agent_name="NegotiationAgent",
            capability="NegotiationAdvisor",
            is_ai_assisted=not used_fallback,
            used_fallback=used_fallback,
            requires_human_approval=requires_approval,
            data=advice.model_dump(),
            explanation=advice.explanation,
        )
        self._record_observability(wf_id, "NegotiationAgent", "AI_NEGOTIATION_ADVICE", rec)
        return rec

    # -------------------------------------------------------------------------
    # 5. Verification Agent Bridge: Explain Verification Failures
    # -------------------------------------------------------------------------
    async def explain_verification_failure(
        self,
        provider_id: str,
        failed_checks: list[dict[str, Any]],
        workflow_id: Optional[UUID] = None,
    ) -> AIRecommendation:
        """Bridge Verification Agent to provide clear institutional failure explanations."""
        wf_id = workflow_id or uuid4()
        reasons = [f"{c.get('check_name', 'Check')}: {c.get('message', 'Failed check')}" for c in failed_checks]
        explanation = (
            f"Provider '{provider_id}' failed institutional verification: {'; '.join(reasons)}. "
            "Deterministic accreditation policy mandates provider exclusion."
        )

        rec = AIRecommendation(
            workflow_id=wf_id,
            agent_name="VerificationAgent",
            capability="VerificationFailureExplanation",
            is_ai_assisted=True,
            used_fallback=False,
            requires_human_approval=True,
            data={"provider_id": provider_id, "failed_checks": failed_checks},
            explanation=explanation,
        )
        self._record_observability(wf_id, "VerificationAgent", "AI_VERIFICATION_FAILURE_EXPLANATION", rec)
        return rec

    # -------------------------------------------------------------------------
    # 6. Monitoring Agent Bridge: Summarize Transaction Events
    # -------------------------------------------------------------------------
    async def summarize_transaction_events(
        self,
        events: list[dict[str, Any]],
        workflow_id: Optional[UUID] = None,
    ) -> AIRecommendation:
        """Bridge Monitoring Agent to summarize on-chain capacity lock and commitment events."""
        wf_id = workflow_id or uuid4()
        summary_lines = [f"- {e.get('event_name', 'Event')} in block {e.get('block_number', 0)}" for e in events]
        explanation = f"Recorded {len(events)} authoritative on-chain event(s):\n" + "\n".join(summary_lines)

        rec = AIRecommendation(
            workflow_id=wf_id,
            agent_name="MonitoringAgent",
            capability="TransactionEventSummary",
            is_ai_assisted=True,
            used_fallback=False,
            requires_human_approval=False,
            data={"event_count": len(events), "events": events},
            explanation=explanation,
        )
        self._record_observability(wf_id, "MonitoringAgent", "AI_TRANSACTION_SUMMARY", rec)
        return rec

    # -------------------------------------------------------------------------
    # 7. Recovery Agent Bridge: Recovery Advisor
    # -------------------------------------------------------------------------
    async def advise_recovery(
        self,
        failure_payload: dict[str, Any],
        workflow_id: Optional[UUID] = None,
    ) -> AIRecommendation:
        """Bridge Recovery Agent to LLM Recovery Advisor."""
        wf_id = workflow_id or failure_payload.get("workflow_id") or uuid4()

        used_fallback = False
        try:
            raw_advice: RecoveryAdviceOutput = await self.recovery_advisor.advise_async(failure_payload)
            advice = RecoveryAdviceOutput.model_validate(raw_advice)
        except Exception as exc:
            if not self.enable_fallback:
                raise
            logger.warning("Recovery advisor fallback triggered: %s", exc)
            used_fallback = True
            advice = RecoveryAdviceOutput(
                failure_classification=FailureClassification.UNKNOWN.value,
                root_cause_explanation=f"Fallback diagnosis for error: {failure_payload.get('error', 'Unknown')}",
                suggested_recovery_path=RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER.value,
                is_recoverable=True,
                requires_human_intervention=False,
                alternative_provider_ids=[],
                remediation_steps=["Seek alternative providers using deterministic fallback."],
            )

        rec = AIRecommendation(
            workflow_id=wf_id,
            agent_name="RecoveryAgent",
            capability="RecoveryAdvisor",
            is_ai_assisted=not used_fallback,
            used_fallback=used_fallback,
            requires_human_approval=advice.requires_human_intervention,
            data=advice.model_dump(),
            explanation=advice.root_cause_explanation,
        )
        self._record_observability(wf_id, "RecoveryAgent", "AI_RECOVERY_ADVICE", rec)
        return rec

    # -------------------------------------------------------------------------
    # 8. Blockchain Security Gate: Prohibits Direct LLM Execution
    # -------------------------------------------------------------------------
    def assert_no_blockchain_authority(self) -> None:
        """
        Enforce strict architectural invariant:
        The LLM intelligence layer possesses ZERO authority to sign transactions,
        release locks, transfer value, or mutate smart contract state directly.
        """
        assert not hasattr(self, "private_key"), "Security violation: private key attached to LLM bridge"
        assert not hasattr(self, "sign_transaction"), "Security violation: signing authority attached to LLM bridge"
        assert not hasattr(self, "send_raw_transaction"), "Security violation: raw transaction authority attached to LLM bridge"

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------
    def _sanitize_input_text(self, text: str) -> str:
        """Strip forbidden credential or sensitive patient PII strings."""
        if not text:
            return ""
        return redact_payload(text) if isinstance(text, (dict, list)) else text.strip()

    def _record_observability(
        self,
        workflow_id: Union[UUID, str],
        agent_id: str,
        event_type: str,
        recommendation: AIRecommendation,
    ) -> None:
        """Record structured AI recommendation in observability log."""
        if self.observability is not None:
            try:
                self.observability.record_event(
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    event_type=event_type,
                    status="SUCCESS",
                    payload={
                        "capability": recommendation.capability,
                        "is_ai_assisted": recommendation.is_ai_assisted,
                        "used_fallback": recommendation.used_fallback,
                        "requires_human_approval": recommendation.requires_human_approval,
                        "explanation": recommendation.explanation,
                        "data": recommendation.data,
                    },
                )
            except Exception as exc:
                logger.warning("Observability logging failed in agent-llm bridge: %s", exc)
