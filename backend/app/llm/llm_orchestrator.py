import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional, Union
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field

from ..agents.discovery_agent import DiscoveryAgent
from ..agents.negotiation_agent import NegotiationAgent
from ..agents.recovery_agent import FailureClassification, RecoveryAgent, RecoveryStrategy
from ..agents.registry import AgentRegistry
from ..agents.request_agent import RequestAgent
from ..schemas.agent_messages import AgentMessage, AgentStatus
from ..schemas.provider_offer import ProviderOffer
from ..schemas.resource_request import (
    LocationArea,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from ..services.agent_observability import AgentObservabilityService
from ..workflows.reservation_workflow import (
    ReservationWorkflowCoordinator,
    ReservationWorkflowResult,
    create_full_coordination_registry,
)
from .client import LLMClient
from .guardrails import LLMGuardrailViolation, LLMOutputValidationError
from .negotiation_advisor import LLMNegotiationAdvisor
from .recovery_advisor import LLMRecoveryAdvisor
from .request_interpreter import LLMRequestParser
from .schemas import (
    LLMError,
    NegotiationAdviceOutput,
    RecoveryAdviceOutput,
    RequestInterpretationOutput,
)

logger = logging.getLogger("medway.llm.orchestrator")


class LLMOrchestrationResult(BaseModel):
    """
    Structured outcome of an AI-coordinated healthcare reservation workflow.
    """
    model_config = ConfigDict(extra="ignore")

    workflow_id: UUID
    input_text: str
    state: str = "INITIALIZED"
    interpretation: Optional[RequestInterpretationOutput] = None
    resource_request: Optional[ResourceRequest] = None
    negotiation_advice: Optional[NegotiationAdviceOutput] = None
    recovery_advice: Optional[RecoveryAdviceOutput] = None
    workflow_result: Optional[ReservationWorkflowResult] = None
    is_ready_for_approval: bool = False
    is_committed: bool = False
    clarification_prompt: Optional[str] = None
    error: Optional[str] = None
    audit_events: list[dict[str, Any]] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LLMOrchestrator:
    """
    MEDway Coordinated AI Reasoning Layer.

    Harmonizes MEDway's three LLM components:
    1. Request Interpreter: Understands natural language healthcare resource requests.
    2. Negotiation Advisor: Evaluates provider offers and trade-offs.
    3. Recovery Advisor: Analyzes workflow failures and recommends recovery strategies.

    Architectural & Governance Invariants:
    1. Advisory Coordination: Coordinates LLM reasoning and passes validated outputs
       to deterministic agents. The LLM never executes privileged blockchain operations.
    2. Attributable Approval Gating: Blockchain commitment strictly requires explicit,
       attributable institutional human approval ('approved_by').
    3. Pydantic Verification: Every LLM output is validated against strict Pydantic schemas.
    4. Deterministic Fallbacks: Unavailability or errors in LLM models trigger graceful
       fallbacks without corrupting workflow integrity.
    5. Audit & Observability Lineage: Preserves workflow correlation IDs across all reasoning
       and agent operations, redacting secrets and patient PII.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        request_parser: Optional[LLMRequestParser] = None,
        negotiation_advisor: Optional[LLMNegotiationAdvisor] = None,
        recovery_advisor: Optional[LLMRecoveryAdvisor] = None,
        workflow_coordinator: Optional[ReservationWorkflowCoordinator] = None,
        observability_service: Optional[AgentObservabilityService] = None,
        enable_fallback: bool = True,
        blockchain_client: Optional[Any] = None,
        blockchain_reader: Optional[Any] = None,
    ) -> None:
        self.client = llm_client or LLMClient()
        self.enable_fallback = enable_fallback
        self.observability = observability_service

        # Initialize the three modular LLM capabilities
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

        self.blockchain_client = blockchain_client
        self.blockchain_reader = blockchain_reader

        # Wire workflow coordinator with LLM-enhanced agent registry
        if workflow_coordinator is not None:
            self.workflow_coordinator = workflow_coordinator
        else:
            registry = self._build_coordinated_registry()
            self.workflow_coordinator = ReservationWorkflowCoordinator(
                registry=registry,
                blockchain_client=blockchain_client,
                blockchain_reader=blockchain_reader,
                observability_service=observability_service,
            )

    def _build_coordinated_registry(self) -> AgentRegistry:
        """Instantiate agent registry and inject LLM-enhanced Request, Negotiation, and Recovery agents."""
        registry = create_full_coordination_registry(
            blockchain_client=self.blockchain_client,
            blockchain_reader=self.blockchain_reader,
        )
        for name in ("RequestAgent", "NegotiationAgent", "RecoveryAgent"):
            if registry.has(name):
                registry.unregister(name)

        registry.register(RequestAgent(parser=self.request_parser))
        registry.register(NegotiationAgent(advisor=self.negotiation_advisor))
        registry.register(RecoveryAgent(registry=registry, advisor=self.recovery_advisor))
        return registry

    async def interpret_request(
        self,
        text: str,
        requester_id: str = "hospital-a",
        workflow_id: Optional[UUID] = None,
    ) -> tuple[RequestInterpretationOutput, Optional[ResourceRequest]]:
        """
        Coordinate natural-language request parsing through Request Interpreter,
        validating structured Pydantic output and logging observability event.
        """
        wf_id = workflow_id or uuid4()
        self._record_observability(
            workflow_id=wf_id,
            agent_id="RequestInterpreter",
            event_type="LLM_REQUEST_INTERPRETATION_STARTED",
            status="SUCCESS",
            payload={"text": text, "requester_id": requester_id},
        )

        try:
            parse_dict = await self.request_parser.parse_async(
                text=text,
                requester_institution_id=requester_id,
            )
        except Exception as exc:
            self._record_observability(
                workflow_id=wf_id,
                agent_id="RequestInterpreter",
                event_type="LLM_REQUEST_INTERPRETATION_FAILED",
                status="FAILED",
                payload={"error": str(exc)},
            )
            raise

        # Build and validate RequestInterpretationOutput
        interpretation_data = parse_dict.get("interpretation")
        if interpretation_data and isinstance(interpretation_data, dict):
            interpretation_output = RequestInterpretationOutput.model_validate(interpretation_data)
        elif isinstance(interpretation_data, RequestInterpretationOutput):
            interpretation_output = interpretation_data
        else:
            rtype = parse_dict.get("resource_type")
            rtype_str = rtype.value if hasattr(rtype, "value") else (str(rtype) if rtype else None)
            urg = parse_dict.get("urgency")
            urg_str = urg.value if hasattr(urg, "value") else (str(urg) if urg else "urgent")
            loc = parse_dict.get("location")
            loc_str = getattr(loc, "city", None) or (str(loc) if loc else None)

            interpretation_output = RequestInterpretationOutput(
                resource_type=rtype_str,
                quantity=parse_dict.get("quantity"),
                urgency=urg_str,
                location=loc_str,
                required_by_hours=parse_dict.get("required_by_hours"),
                is_ambiguous=bool(parse_dict.get("ambiguous")),
                missing_fields=list(parse_dict.get("missing_fields") or []),
                clarification_prompt=parse_dict.get("clarification_prompt"),
            )

        # Log completion
        self._record_observability(
            workflow_id=wf_id,
            agent_id="RequestInterpreter",
            event_type="LLM_REQUEST_INTERPRETATION_COMPLETED",
            status="SUCCESS",
            payload=interpretation_output.model_dump(),
        )

        # Check if complete ResourceRequest can be constructed
        resource_request: Optional[ResourceRequest] = None
        if (
            not interpretation_output.is_ambiguous
            and not interpretation_output.missing_fields
            and interpretation_output.resource_type
            and interpretation_output.quantity
        ):
            now = datetime.now(timezone.utc)
            req_hours = interpretation_output.required_by_hours or 24
            deadline = now + timedelta(hours=req_hours)
            resource_request = ResourceRequest(
                workflow_id=wf_id,
                requester_institution_id=requester_id,
                resource_type=ResourceType(interpretation_output.resource_type.lower().replace(" ", "_")),
                quantity=interpretation_output.quantity,
                urgency=UrgencyLevel(interpretation_output.urgency.lower() if interpretation_output.urgency else "urgent"),
                location=LocationArea(city=interpretation_output.location, service_area=interpretation_output.location) if interpretation_output.location else None,
                required_by=deadline,
                required_by_hours=req_hours,
                created_at=now,
            )

        return interpretation_output, resource_request

    async def advise_negotiation(
        self,
        request: ResourceRequest,
        offers: list[ProviderOffer],
        workflow_id: Optional[UUID] = None,
        max_budget: Optional[float] = None,
    ) -> NegotiationAdviceOutput:
        """
        Coordinate offer evaluation through Negotiation Advisor,
        validating structured Pydantic output and logging observability event.
        """
        wf_id = workflow_id or request.workflow_id or uuid4()
        self._record_observability(
            workflow_id=wf_id,
            agent_id="NegotiationAdvisor",
            event_type="LLM_NEGOTIATION_ADVICE_STARTED",
            status="SUCCESS",
            payload={"offers_count": len(offers), "max_budget": max_budget},
        )

        advice: NegotiationAdviceOutput = await self.negotiation_advisor.advise_async(
            request=request,
            offers=offers,
            max_budget=max_budget,
        )

        validated_advice = NegotiationAdviceOutput.model_validate(advice)

        self._record_observability(
            workflow_id=wf_id,
            agent_id="NegotiationAdvisor",
            event_type="LLM_NEGOTIATION_ADVICE_COMPLETED",
            status="SUCCESS",
            payload=validated_advice.model_dump(),
        )

        return validated_advice

    async def advise_recovery(
        self,
        failure_payload: dict[str, Any],
        workflow_id: Optional[UUID] = None,
    ) -> RecoveryAdviceOutput:
        """
        Coordinate failure diagnosis through Recovery Advisor,
        validating structured Pydantic output and logging observability event.
        """
        wf_id = workflow_id or failure_payload.get("workflow_id") or uuid4()
        self._record_observability(
            workflow_id=wf_id,
            agent_id="RecoveryAdvisor",
            event_type="LLM_RECOVERY_ADVICE_STARTED",
            status="SUCCESS",
            payload={"error": str(failure_payload.get("error"))},
        )

        advice: RecoveryAdviceOutput = await self.recovery_advisor.advise_async(
            failure_payload=failure_payload,
        )

        validated_advice = RecoveryAdviceOutput.model_validate(advice)

        self._record_observability(
            workflow_id=wf_id,
            agent_id="RecoveryAdvisor",
            event_type="LLM_RECOVERY_ADVICE_COMPLETED",
            status="SUCCESS",
            payload=validated_advice.model_dump(),
        )

        return validated_advice

    async def coordinate(
        self,
        natural_language_request: str,
        workflow_id: Optional[UUID] = None,
        requester_id: str = "hospital-a",
        approved_by: Optional[str] = None,
    ) -> LLMOrchestrationResult:
        """
        Execute full AI-assisted healthcare coordination:
        1. Interpret natural language request with Request Interpreter.
        2. If missing information or ambiguous, pause for clarification.
        3. Pass validated request to workflow coordinator (Discovery, Offers, Negotiation, Verification).
        4. If approved_by is provided, execute on-chain commit via BlockchainExecutionAgent.
        5. If failure occurs, invoke Recovery Advisor.
        6. Return complete Pydantic orchestration outcome.
        """
        wf_id = workflow_id or uuid4()
        now = datetime.now(timezone.utc)

        self._record_observability(
            workflow_id=wf_id,
            agent_id="LLMOrchestrator",
            event_type="COORDINATION_STARTED",
            status="SUCCESS",
            payload={"input_text": natural_language_request, "requester_id": requester_id},
        )

        # 1. Request Interpretation
        interpretation, resource_request = await self.interpret_request(
            text=natural_language_request,
            requester_id=requester_id,
            workflow_id=wf_id,
        )

        if interpretation.is_ambiguous or interpretation.missing_fields or not resource_request:
            prompt = interpretation.clarification_prompt or "Please clarify required resource type, quantity, or location."
            self._record_observability(
                workflow_id=wf_id,
                agent_id="LLMOrchestrator",
                event_type="COORDINATION_NEEDS_CLARIFICATION",
                status="SUCCESS",
                payload={"clarification_prompt": prompt, "missing_fields": interpretation.missing_fields},
            )
            return LLMOrchestrationResult(
                workflow_id=wf_id,
                input_text=natural_language_request,
                state="NEEDS_CLARIFICATION",
                interpretation=interpretation,
                resource_request=None,
                is_ready_for_approval=False,
                is_committed=False,
                clarification_prompt=prompt,
            )

        # 2. Invoke Existing Workflow Coordinator
        wf_result = await self.workflow_coordinator.start(
            input_data=resource_request,
            workflow_id=wf_id,
        )

        # 3. Extract or enrich Negotiation Advice
        negotiation_advice: Optional[NegotiationAdviceOutput] = None
        if wf_result.negotiation_advice:
            try:
                negotiation_advice = NegotiationAdviceOutput.model_validate(wf_result.negotiation_advice)
            except Exception:
                pass
        elif wf_result.provider_offers:
            # Fallback advice invocation
            try:
                negotiation_advice = await self.advise_negotiation(
                    request=resource_request,
                    offers=wf_result.provider_offers,
                    workflow_id=wf_id,
                )
            except Exception as exc:
                logger.warning("Negotiation advice enrichment error: %s", exc)

        # 4. Extract or enrich Recovery Advice if failures occurred
        recovery_advice: Optional[RecoveryAdviceOutput] = None
        if wf_result.recovery_attempts:
            last_att = wf_result.recovery_attempts[-1]
            if last_att.get("recovery_advice"):
                try:
                    recovery_advice = RecoveryAdviceOutput.model_validate(last_att["recovery_advice"])
                except Exception:
                    pass
            elif wf_result.state in ("ESCALATED", "FAILED", "NO_PROVIDERS_FOUND"):
                try:
                    recovery_advice = await self.advise_recovery(
                        failure_payload={
                            "error": last_att.get("error", wf_result.error or "Workflow coordination halted"),
                            "failure_classification": last_att.get("classification"),
                            "workflow_id": str(wf_id),
                        },
                        workflow_id=wf_id,
                    )
                except Exception as exc:
                    logger.warning("Recovery advice enrichment error: %s", exc)

        # 5. Attributable Approval Gating Check
        # Prevents LLM recommendations from executing privileged transactions without human approval
        if approved_by and str(approved_by).strip() and wf_result.is_ready_for_approval:
            wf_result = await self.workflow_coordinator.authorize_and_commit(
                workflow_id=wf_id,
                approved_by=approved_by,
                operation="LOCK_AND_COMMIT",
            )

        self._record_observability(
            workflow_id=wf_id,
            agent_id="LLMOrchestrator",
            event_type="COORDINATION_COMPLETED",
            status="SUCCESS",
            payload={"final_state": wf_result.state, "is_committed": wf_result.is_committed},
        )

        return LLMOrchestrationResult(
            workflow_id=wf_id,
            input_text=natural_language_request,
            state=wf_result.state,
            interpretation=interpretation,
            resource_request=resource_request,
            negotiation_advice=negotiation_advice,
            recovery_advice=recovery_advice,
            workflow_result=wf_result,
            is_ready_for_approval=wf_result.is_ready_for_approval,
            is_committed=wf_result.is_committed,
            clarification_prompt=wf_result.clarification_prompt,
            error=wf_result.error,
            audit_events=list(wf_result.audit_trail),
        )

    async def authorize_and_commit(
        self,
        workflow_id: UUID,
        approved_by: str,
        operation: str = "LOCK_AND_COMMIT",
    ) -> LLMOrchestrationResult:
        """
        Record attributable institutional human approval and execute on-chain
        capacity locking and reservation commitment.
        """
        if not approved_by or not str(approved_by).strip():
            raise ValueError("Institutional human approval ('approved_by') is strictly required for blockchain execution.")

        wf_result = await self.workflow_coordinator.authorize_and_commit(
            workflow_id=workflow_id,
            approved_by=approved_by,
            operation=operation,
        )

        self._record_observability(
            workflow_id=workflow_id,
            agent_id="LLMOrchestrator",
            event_type="COMMITMENT_AUTHORIZED_BY_HUMAN",
            status="SUCCESS",
            payload={"approved_by": approved_by, "is_committed": wf_result.is_committed},
        )

        return LLMOrchestrationResult(
            workflow_id=workflow_id,
            input_text="Institutional Commitment Authorization",
            state=wf_result.state,
            workflow_result=wf_result,
            is_ready_for_approval=wf_result.is_ready_for_approval,
            is_committed=wf_result.is_committed,
            audit_events=list(wf_result.audit_trail),
        )

    def _record_observability(
        self,
        workflow_id: Union[UUID, str],
        agent_id: str,
        event_type: str,
        status: str = "SUCCESS",
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Safely record observability telemetry without throwing exceptions."""
        if self.observability is not None:
            try:
                self.observability.record_event(
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    event_type=event_type,
                    status=status,
                    payload=payload,
                )
            except Exception as exc:
                logger.warning("Observability logging failure in orchestrator: %s", exc)
