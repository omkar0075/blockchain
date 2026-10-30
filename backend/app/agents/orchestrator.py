import asyncio
from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Optional, Union
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field

from .base import BaseAgent
from .context import AgentContext
from .discovery_agent import DiscoveryAgent
from .negotiation_agent import NegotiationAgent
from .provider_agent import ProviderAgent
from .registry import AgentRegistry, default_registry
from .request_agent import RequestAgent
from .verification_agent import VerificationAgent
from ..schemas.agent_messages import (
    AgentError,
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from ..schemas.negotiation import NegotiationProposal
from ..schemas.provider_offer import ProviderOffer
from ..schemas.resource_request import LocationArea, ResourceRequest
from ..schemas.verification import VerificationResult, VerificationStatus

logger = logging.getLogger("medway.agents.orchestrator")


class WorkflowState(str, Enum):
    """
    Explicit, deterministic lifecycle states for a MEDway coordination workflow.
    """
    INITIALIZED = "INITIALIZED"
    PARSING_REQUEST = "PARSING_REQUEST"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    DISCOVERING_PROVIDERS = "DISCOVERING_PROVIDERS"
    NO_PROVIDERS_FOUND = "NO_PROVIDERS_FOUND"
    REQUESTING_OFFERS = "REQUESTING_OFFERS"
    NO_OFFERS_RECEIVED = "NO_OFFERS_RECEIVED"
    NEGOTIATING = "NEGOTIATING"
    NEGOTIATION_FAILED = "NEGOTIATION_FAILED"
    VERIFYING = "VERIFYING"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    APPROVED = "APPROVED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class WorkflowTransition(BaseModel):
    """Immutable audit record of a workflow state transition."""
    model_config = ConfigDict(extra="ignore")

    from_state: WorkflowState
    to_state: WorkflowState
    agent: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: Optional[str] = None


class WorkflowRecord(BaseModel):
    """
    Complete audit snapshot of an executed multi-agent coordination workflow.
    """
    model_config = ConfigDict(extra="ignore")

    workflow_id: UUID
    state: WorkflowState
    transitions: list[WorkflowTransition] = Field(default_factory=list)
    request: Optional[ResourceRequest] = None
    context: Optional[AgentContext] = None
    discovered_candidates: list[dict[str, Any]] = Field(default_factory=list)
    provider_offers: list[ProviderOffer] = Field(default_factory=list)
    selected_proposal: Optional[NegotiationProposal] = None
    selected_offer: Optional[ProviderOffer] = None
    verification_result: Optional[VerificationResult] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    clarification_prompt: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def create_default_registry() -> AgentRegistry:
    """Instantiate a standard registry populated with the primary MEDway agents."""
    registry = AgentRegistry()
    registry.register(RequestAgent())
    registry.register(DiscoveryAgent())
    registry.register(NegotiationAgent())
    registry.register(VerificationAgent())

    # Standard default accredited providers
    registry.register(
        ProviderAgent(
            provider_id="hospital-b",
            provider_name="Community Hospital B",
            institution_type="hospital",
            location=LocationArea(city="Metropolis", postal_code="560001", service_area="Metropolis"),
            inventory={
                "diagnostic_slot": {"total": 4, "available": 4},
                "icu_bed": {"total": 6, "available": 3},
                "hospital_bed": {"total": 20, "available": 10},
            },
            delivery_hours=4,
        )
    )
    registry.register(
        ProviderAgent(
            provider_id="lab-c",
            provider_name="Central Diagnostic Lab C",
            institution_type="diagnostic_lab",
            location=LocationArea(city="Metropolis", postal_code="560001", service_area="Metropolis"),
            inventory={
                "diagnostic_slot": {"total": 12, "available": 8},
            },
            delivery_hours=2,
        )
    )
    return registry


class WorkflowOrchestrator:
    """
    Multi-Agent Workflow Orchestrator.

    Coordinates Request Understanding, Provider Discovery, Provider Offers,
    Multi-Lateral Negotiation, and Independent Verification through a deterministic
    state machine.

    Architectural & Governance Invariants:
    1. Deterministic State Machine: Agents supply recommendations; the orchestrator
       strictly controls transitions and terminal states.
    2. Zero Automatic Commitment: Reaches WAITING_FOR_APPROVAL after verification;
       never executes blockchain commitments without attributable institutional approval.
    3. Failure & Verification Integrity: Failures halt progress; verification can never
       be bypassed or skipped.
    4. Lineage Preservation: Preserves the same workflow_id across all messages and records.
    5. Duplicate Protection: Rejects duplicate runs for the same workflow_id.
    6. Auditability: Records every state transition with UTC timestamps and agent attribution.
    """

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        step_timeout_seconds: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        self.registry = registry if registry is not None else create_default_registry()
        self.step_timeout_seconds = step_timeout_seconds
        self.max_retries = max_retries
        self._workflows: dict[UUID, WorkflowRecord] = {}
        self._running_workflows: set[UUID] = set()

    def get_workflow(self, workflow_id: UUID) -> Optional[WorkflowRecord]:
        """Retrieve historical or active workflow record."""
        return self._workflows.get(workflow_id)

    async def run(
        self,
        input_data: Union[str, dict[str, Any], ResourceRequest],
        workflow_id: Optional[UUID] = None,
    ) -> WorkflowRecord:
        """
        Execute an end-to-end multi-agent coordination workflow.

        Args:
            input_data: Natural language request string, raw dictionary, or validated ResourceRequest.
            workflow_id: Optional unique workflow identifier. Generated if omitted.

        Returns:
            WorkflowRecord capturing the full execution audit trail and terminal/paused state.
        """
        wf_id = workflow_id or uuid4()

        # Invariant 5: Duplicate Protection
        if wf_id in self._workflows or wf_id in self._running_workflows:
            raise ValueError(f"Workflow '{wf_id}' has already been executed or is currently running")

        self._running_workflows.add(wf_id)
        now = datetime.now(timezone.utc)

        record = WorkflowRecord(
            workflow_id=wf_id,
            state=WorkflowState.INITIALIZED,
            created_at=now,
            updated_at=now,
        )
        self._workflows[wf_id] = record

        try:
            # -------------------------------------------------------------
            # Stage 1: Request Parsing & Validation
            # -------------------------------------------------------------
            request: ResourceRequest

            if isinstance(input_data, ResourceRequest):
                request = input_data
                self._transition(record, WorkflowState.PARSING_REQUEST, agent="System", details="Pre-parsed ResourceRequest provided")
            else:
                self._transition(record, WorkflowState.PARSING_REQUEST, agent="RequestAgent", details="Initiating request extraction")
                req_agent = self.registry.get("RequestAgent")

                raw_payload: dict[str, Any]
                if isinstance(input_data, str):
                    raw_payload = {"text": input_data}
                elif isinstance(input_data, dict):
                    raw_payload = input_data
                else:
                    raise TypeError(f"Unsupported input_data type: {type(input_data).__name__}")

                msg = AgentMessage(
                    workflow_id=wf_id,
                    sender="WorkflowOrchestrator",
                    receiver=req_agent.name,
                    message_type="PARSE_REQUEST",
                    payload=raw_payload,
                )

                parse_res = await self._execute_with_retry(req_agent, msg)
                if parse_res.status == AgentStatus.FAILED:
                    err_msg = parse_res.error.message if parse_res.error else "RequestAgent execution failed"
                    record.error = err_msg
                    self._transition(record, WorkflowState.FAILED, agent="RequestAgent", details=err_msg)
                    return record

                if parse_res.data.get("clarification_needed") or parse_res.data.get("needs_clarification"):
                    clarification = parse_res.data.get("clarification_prompt", "Please clarify your request.")
                    record.clarification_prompt = clarification
                    self._transition(
                        record,
                        WorkflowState.NEEDS_CLARIFICATION,
                        agent="RequestAgent",
                        details=clarification,
                    )
                    return record

                req_data = parse_res.data.get("resource_request") or parse_res.data.get("request")
                if not req_data:
                    err_msg = parse_res.data.get("message") or "Request parsing failed to produce resource_request"
                    record.error = err_msg
                    self._transition(record, WorkflowState.FAILED, agent="RequestAgent", details=err_msg)
                    return record

                request = ResourceRequest.model_validate(req_data)

            record.request = request
            record.context = AgentContext(
                workflow_id=wf_id,
                request=request,
                current_agent="WorkflowOrchestrator",
            )

            # -------------------------------------------------------------
            # Stage 2: Provider Discovery
            # -------------------------------------------------------------
            self._transition(record, WorkflowState.DISCOVERING_PROVIDERS, agent="DiscoveryAgent", details="Searching accredited providers")
            record.context.update_agent("DiscoveryAgent")

            discovery_agent = self.registry.get("DiscoveryAgent")
            disc_msg = AgentMessage(
                workflow_id=wf_id,
                sender="WorkflowOrchestrator",
                receiver=discovery_agent.name,
                message_type="DISCOVER_PROVIDERS",
                payload={"resource_request": request.model_dump()},
            )

            disc_res = await self._execute_with_retry(discovery_agent, disc_msg)
            if disc_res.status == AgentStatus.FAILED:
                err_msg = disc_res.error.message if disc_res.error else "DiscoveryAgent failed"
                record.error = err_msg
                self._transition(record, WorkflowState.FAILED, agent="DiscoveryAgent", details=err_msg)
                return record

            candidates = disc_res.data.get("candidate_offers") or disc_res.data.get("candidates") or []
            record.discovered_candidates = candidates

            if not candidates or not disc_res.data.get("success"):
                reason = disc_res.data.get("message", "No matching providers found in service area")
                self._transition(record, WorkflowState.NO_PROVIDERS_FOUND, agent="DiscoveryAgent", details=reason)
                return record

            # -------------------------------------------------------------
            # Stage 3: Request Provider Offers
            # -------------------------------------------------------------
            self._transition(record, WorkflowState.REQUESTING_OFFERS, agent="WorkflowOrchestrator", details=f"Querying {len(candidates)} provider(s)")
            collected_offers: list[ProviderOffer] = []

            for candidate in candidates:
                p_id = candidate.get("id") or candidate.get("provider_id")
                # Locate provider agent in registry
                p_agent = None
                for key_variant in (f"ProviderAgent_{p_id}", p_id, "ProviderAgent"):
                    try:
                        p_agent = self.registry.get(key_variant)
                        break
                    except KeyError:
                        continue

                if not p_agent:
                    logger.info("No registered ProviderAgent found for candidate '%s'; skipping", p_id)
                    continue

                p_msg = AgentMessage(
                    workflow_id=wf_id,
                    sender="WorkflowOrchestrator",
                    receiver=p_agent.name,
                    message_type="REQUEST_RESOURCE",
                    payload={"resource_request": request.model_dump()},
                )

                p_res = await self._execute_with_retry(p_agent, p_msg)
                if p_res.status == AgentStatus.COMPLETED and p_res.data.get("success") and p_res.data.get("offer"):
                    offer_obj = ProviderOffer.model_validate(p_res.data["offer"])
                    collected_offers.append(offer_obj)

            record.provider_offers = collected_offers
            if not collected_offers:
                self._transition(
                    record,
                    WorkflowState.NO_OFFERS_RECEIVED,
                    agent="WorkflowOrchestrator",
                    details="None of the discovered candidate providers returned a viable offer",
                )
                return record

            # -------------------------------------------------------------
            # Stage 4: Multi-Lateral Negotiation
            # -------------------------------------------------------------
            self._transition(record, WorkflowState.NEGOTIATING, agent="NegotiationAgent", details=f"Evaluating {len(collected_offers)} offer(s)")
            record.context.update_agent("NegotiationAgent")

            negotiation_agent = self.registry.get("NegotiationAgent")
            neg_msg = AgentMessage(
                workflow_id=wf_id,
                sender="WorkflowOrchestrator",
                receiver=negotiation_agent.name,
                message_type="NEGOTIATE_OFFERS",
                payload={
                    "resource_request": request.model_dump(),
                    "offers": [o.model_dump() for o in collected_offers],
                    "history": [],
                },
            )

            neg_res = await self._execute_with_retry(negotiation_agent, neg_msg)
            if neg_res.status == AgentStatus.FAILED:
                err_msg = neg_res.error.message if neg_res.error else "NegotiationAgent failed"
                record.error = err_msg
                self._transition(record, WorkflowState.FAILED, agent="NegotiationAgent", details=err_msg)
                return record

            proposal_raw = neg_res.data.get("proposal")
            if not proposal_raw:
                reason = neg_res.data.get("message", "Negotiation policy rejected all available offers")
                self._transition(record, WorkflowState.NEGOTIATION_FAILED, agent="NegotiationAgent", details=reason)
                return record

            proposal = NegotiationProposal.model_validate(proposal_raw)
            selected_offer = ProviderOffer.model_validate(neg_res.data["selected_offer"])
            record.selected_proposal = proposal
            record.selected_offer = selected_offer

            # -------------------------------------------------------------
            # Stage 5: Independent Verification
            # -------------------------------------------------------------
            self._transition(record, WorkflowState.VERIFYING, agent="VerificationAgent", details=f"Verifying provider '{selected_offer.provider_id}'")
            record.context.update_agent("VerificationAgent")

            verification_agent = self.registry.get("VerificationAgent")
            ver_msg = AgentMessage(
                workflow_id=wf_id,
                sender="WorkflowOrchestrator",
                receiver=verification_agent.name,
                message_type="VERIFY_OFFER",
                payload={
                    "resource_request": request.model_dump(),
                    "provider_offer": selected_offer.model_dump(),
                },
            )

            ver_res = await self._execute_with_retry(verification_agent, ver_msg)
            if ver_res.status == AgentStatus.FAILED:
                err_msg = ver_res.error.message if ver_res.error else "VerificationAgent failed"
                record.error = err_msg
                self._transition(record, WorkflowState.FAILED, agent="VerificationAgent", details=err_msg)
                return record

            v_raw = ver_res.data.get("verification")
            if not v_raw:
                record.error = "VerificationAgent produced no verification record"
                self._transition(record, WorkflowState.FAILED, agent="VerificationAgent", details="Missing verification record")
                return record

            verification = VerificationResult.model_validate(v_raw)
            record.verification_result = verification

            # Invariant 3: Failure & Verification Integrity
            if verification.status == VerificationStatus.REJECTED:
                reasons = "; ".join(f"{fc.check_name}: {fc.message}" for fc in verification.failed_checks)
                self._transition(
                    record,
                    WorkflowState.VERIFICATION_FAILED,
                    agent="VerificationAgent",
                    details=f"Verification failed: {reasons}",
                )
                return record

            if verification.status == VerificationStatus.REQUIRES_REVIEW:
                self._transition(
                    record,
                    WorkflowState.REQUIRES_REVIEW,
                    agent="VerificationAgent",
                    details=verification.explanation or "Authoritative capacity inconclusive. Administrative review required.",
                )
                return record

            # -------------------------------------------------------------
            # Stage 6: Waiting For Institutional Approval
            # -------------------------------------------------------------
            # Invariant 2: Verification completes, but commits require institutional approval
            self._transition(
                record,
                WorkflowState.WAITING_FOR_APPROVAL,
                agent="WorkflowOrchestrator",
                details="Verification passed. Awaiting attributable institutional human approval before commitment.",
            )
            return record

        finally:
            self._running_workflows.discard(wf_id)

    def record_approval(self, workflow_id: UUID, approved_by: str) -> WorkflowRecord:
        """
        Record attributable human institutional authorization for a verified allocation.
        """
        cleaned_approver = approved_by.strip() if approved_by else ""
        if not cleaned_approver:
            raise ValueError("approved_by cannot be empty or whitespace only")

        record = self._workflows.get(workflow_id)
        if not record:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        if record.state != WorkflowState.WAITING_FOR_APPROVAL:
            raise ValueError(
                f"Cannot approve workflow '{workflow_id}' in state '{record.state.value}'. "
                f"Must be in '{WorkflowState.WAITING_FOR_APPROVAL.value}'"
            )

        now = datetime.now(timezone.utc)
        record.approved_by = cleaned_approver
        record.approved_at = now

        if record.context:
            record.context.record_approval(cleaned_approver, now)

        self._transition(
            record,
            WorkflowState.APPROVED,
            agent="InstitutionalOfficer",
            details=f"Authorized by {cleaned_approver}",
        )
        return record

    def execute_blockchain_commitment(self, workflow_id: UUID) -> None:
        """
        Enforce Governance Invariant: Blockchain execution requires institutional approval.
        Actual blockchain calls are reserved for the Phase 4 smart contract integration.
        """
        record = self._workflows.get(workflow_id)
        if not record:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        if record.state != WorkflowState.APPROVED or not record.approved_by:
            raise PermissionError(
                f"Governance violation: cannot execute blockchain commitment for workflow '{workflow_id}' "
                f"in state '{record.state.value}'. Attributable institutional approval is required."
            )

        # Invariant 8: Do not execute blockchain transactions in this task
        raise NotImplementedError(
            "Direct on-chain capacity locking is reserved for Phase 4 Smart Contract Integration."
        )

    def _transition(
        self,
        record: WorkflowRecord,
        to_state: WorkflowState,
        agent: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        """Record and apply an audit-logged workflow state transition."""
        from_state = record.state
        transition = WorkflowTransition(
            from_state=from_state,
            to_state=to_state,
            agent=agent,
            timestamp=datetime.now(timezone.utc),
            details=details,
        )
        record.transitions.append(transition)
        record.state = to_state
        record.updated_at = datetime.now(timezone.utc)
        logger.info(
            "Workflow %s transitioned: %s -> %s (by %s): %s",
            record.workflow_id,
            from_state.value,
            to_state.value,
            agent,
            details,
        )

    async def _execute_with_retry(
        self,
        agent: BaseAgent,
        message: AgentMessage,
    ) -> AgentResult:
        """Invoke an agent step with timeout protection and transient error retries."""
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    agent.execute(message),
                    timeout=self.step_timeout_seconds,
                )
                return result
            except asyncio.TimeoutError as exc:
                last_error = exc
                logger.warning(
                    "Agent '%s' timed out (attempt %d/%d) for workflow %s",
                    agent.name,
                    attempt + 1,
                    self.max_retries + 1,
                    message.workflow_id,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Agent '%s' raised exception (attempt %d/%d) for workflow %s: %s",
                    agent.name,
                    attempt + 1,
                    self.max_retries + 1,
                    message.workflow_id,
                    exc,
                )

        return AgentResult(
            workflow_id=message.workflow_id,
            agent_name=agent.name,
            status=AgentStatus.FAILED,
            error=AgentError(
                error_code="EXECUTION_TIMEOUT_OR_FAILURE",
                message=f"Agent '{agent.name}' failed after {self.max_retries + 1} attempts: {last_error}",
                agent_name=agent.name,
                workflow_id=message.workflow_id,
                recoverable=False,
            ),
        )
