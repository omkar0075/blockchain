import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional, Union
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field

from ..agents.blockchain_agent import BlockchainExecutionAgent, DefaultBlockchainClient
from ..agents.context import AgentContext
from ..agents.discovery_agent import DiscoveryAgent
from ..agents.monitoring_agent import (
    BlockchainEvent,
    DefaultBlockchainReader,
    MonitoringAgent,
    ReservationLifecycleState,
)
from ..agents.negotiation_agent import NegotiationAgent
from ..agents.provider_agent import ProviderAgent
from ..agents.recovery_agent import (
    FailureClassification,
    RecoveryAgent,
    RecoveryStrategy,
)
from ..agents.registry import AgentRegistry
from ..agents.request_agent import RequestAgent
from ..agents.verification_agent import VerificationAgent
from ..schemas.agent_messages import (
    AgentError,
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from ..schemas.negotiation import NegotiationProposal
from ..schemas.provider_offer import ProviderOffer
from ..schemas.resource_request import LocationArea, ResourceRequest, ResourceType
from ..schemas.verification import VerificationResult, VerificationStatus

logger = logging.getLogger("medway.workflows.reservation")


class ReservationWorkflowResult(BaseModel):
    """
    End-to-end synchronized audit snapshot of a healthcare coordination workflow.
    """
    model_config = ConfigDict(extra="ignore")

    workflow_id: UUID
    state: str = "INITIALIZED"
    request: Optional[ResourceRequest] = None
    context: Optional[AgentContext] = None
    discovered_candidates: list[dict[str, Any]] = Field(default_factory=list)
    provider_offers: list[ProviderOffer] = Field(default_factory=list)
    selected_offer: Optional[ProviderOffer] = None
    selected_proposal: Optional[NegotiationProposal] = None
    verification: Optional[VerificationResult] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    onchain_result: Optional[dict[str, Any]] = None
    monitoring_status: Optional[str] = None
    clarification_prompt: Optional[str] = None
    recovery_attempts: list[dict[str, Any]] = Field(default_factory=list)
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    is_ready_for_approval: bool = False
    is_committed: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def create_full_coordination_registry(
    blockchain_client: Optional[Any] = None,
    blockchain_reader: Optional[Any] = None,
) -> AgentRegistry:
    """Instantiate and register all nine autonomous agents into a dedicated registry."""
    registry = AgentRegistry()

    # Core agents
    registry.register(RequestAgent())
    registry.register(DiscoveryAgent())
    registry.register(NegotiationAgent())
    registry.register(VerificationAgent())
    registry.register(BlockchainExecutionAgent(blockchain_client=blockchain_client))
    registry.register(MonitoringAgent(reader=blockchain_reader))
    registry.register(RecoveryAgent(registry=registry))

    # Standard accredited provider agents
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


class ReservationWorkflowCoordinator:
    """
    End-to-End Healthcare Resource Reservation Workflow Coordinator.

    Orchestrates the complete multi-agent lifecycle:
    1. Natural-language request parsing and extraction
    2. Candidate provider discovery
    3. Multi-lateral provider offer collection
    4. Deterministic multi-criteria negotiation
    5. Independent accreditation and capacity verification
    6. Attributable institutional human approval gating
    7. On-chain 2PC capacity locking and reservation commitment
    8. Event-driven confirmation monitoring and synchronization
    9. Automated root-cause recovery and alternative discovery

    Governance Invariants:
    - Never executes blockchain transactions without attributable institutional approval.
    - An advertised offer is NEVER treated as proof of reservation.
    - Recovered alternatives strictly require fresh verification and approval.
    - Active commitments cannot be silently cancelled.
    """

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        blockchain_client: Optional[Any] = None,
        blockchain_reader: Optional[Any] = None,
        max_recovery_attempts: int = 3,
    ) -> None:
        self.registry = registry or create_full_coordination_registry(
            blockchain_client=blockchain_client,
            blockchain_reader=blockchain_reader,
        )
        self.max_recovery_attempts = max_recovery_attempts
        self._workflows: dict[UUID, ReservationWorkflowResult] = {}
        self._active_runs: set[UUID] = set()

    def get_workflow(self, workflow_id: UUID) -> Optional[ReservationWorkflowResult]:
        """Retrieve existing workflow state and audit record."""
        return self._workflows.get(workflow_id)

    async def start(
        self,
        input_data: Union[str, dict[str, Any], ResourceRequest],
        workflow_id: Optional[UUID] = None,
    ) -> ReservationWorkflowResult:
        """
        Initiate the coordination pipeline through discovery, negotiation, and verification,
        stopping safely at WAITING_FOR_APPROVAL.
        """
        wf_id = workflow_id or uuid4()

        if wf_id in self._workflows or wf_id in self._active_runs:
            raise ValueError(f"Workflow '{wf_id}' is already active or completed")

        self._active_runs.add(wf_id)
        now = datetime.now(timezone.utc)

        result = ReservationWorkflowResult(
            workflow_id=wf_id,
            state="INITIALIZED",
            created_at=now,
            updated_at=now,
        )
        self._workflows[wf_id] = result

        try:
            # ---------------------------------------------------------
            # Stage 1: Request Parsing & Validation
            # ---------------------------------------------------------
            request: ResourceRequest

            if isinstance(input_data, ResourceRequest):
                request = input_data
                self._record_step(result, "PARSING_REQUEST", "System", "Pre-validated ResourceRequest provided")
            else:
                self._record_step(result, "PARSING_REQUEST", "RequestAgent", "Initiating natural language parsing")
                req_agent = self.registry.get("RequestAgent")

                payload_data: dict[str, Any] = {"text": input_data} if isinstance(input_data, str) else input_data
                parse_msg = AgentMessage(
                    workflow_id=wf_id,
                    sender="ReservationWorkflow",
                    receiver="RequestAgent",
                    message_type="PARSE_REQUEST",
                    payload=payload_data,
                )
                parse_res = await req_agent.execute(parse_msg)

                if parse_res.status == AgentStatus.FAILED:
                    err = parse_res.error.message if parse_res.error else "Request parsing failed"
                    result.error = err
                    self._record_step(result, "FAILED", "RequestAgent", err)
                    return result

                if parse_res.data.get("clarification_needed") or parse_res.data.get("needs_clarification"):
                    prompt = parse_res.data.get("clarification_prompt", "Please clarify required quantity or location.")
                    result.clarification_prompt = prompt
                    self._record_step(result, "NEEDS_CLARIFICATION", "RequestAgent", prompt)
                    return result

                req_dict = parse_res.data.get("resource_request") or parse_res.data.get("request")
                if not req_dict:
                    result.error = "Missing resource_request in parse response"
                    self._record_step(result, "FAILED", "RequestAgent", result.error)
                    return result

                request = ResourceRequest.model_validate(req_dict)

            result.request = request
            result.context = AgentContext(
                workflow_id=wf_id,
                request=request,
                current_agent="ReservationWorkflow",
            )

            # ---------------------------------------------------------
            # Stages 2 - 5: Discovery, Offers, Negotiation, Verification
            # (With automatic recovery if any stage fails)
            # ---------------------------------------------------------
            await self._run_discovery_negotiation_verification(result, request)
            return result

        finally:
            self._active_runs.discard(wf_id)

    async def _run_discovery_negotiation_verification(
        self,
        result: ReservationWorkflowResult,
        request: ResourceRequest,
    ) -> None:
        """Run the core selection loop with automated failure recovery."""
        wf_id = result.workflow_id
        failed_providers: set[str] = set()

        for attempt in range(1, self.max_recovery_attempts + 1):
            # 1. Discovery
            self._record_step(result, "DISCOVERING_PROVIDERS", "DiscoveryAgent", f"Searching providers (attempt {attempt})")
            disc_agent = self.registry.get("DiscoveryAgent")

            disc_msg = AgentMessage(
                workflow_id=wf_id,
                sender="ReservationWorkflow",
                receiver="DiscoveryAgent",
                message_type="DISCOVER_PROVIDERS",
                payload={"resource_request": request.model_dump()},
            )
            disc_res = await disc_agent.execute(disc_msg)

            candidates = (
                disc_res.data.get("candidate_offers")
                or disc_res.data.get("candidates")
                or []
            )
            result.discovered_candidates = candidates

            # Filter out any providers that failed previously in this workflow
            active_candidates = [
                c for c in candidates
                if (c.get("provider_id") or c.get("id")) not in failed_providers
            ]

            if not active_candidates:
                # Trigger Recovery Agent to diagnose lack of alternatives
                recovery_res = await self._invoke_recovery(
                    result=result,
                    error=f"No eligible providers remaining after failures: {failed_providers}",
                    classification=FailureClassification.INSUFFICIENT_CAPACITY,
                    failed_provider=list(failed_providers)[-1] if failed_providers else None,
                    attempt=attempt,
                )
                if recovery_res.get("escalated") or failed_providers:
                    self._record_step(result, "ESCALATED", "RecoveryAgent", recovery_res.get("explanation") or "All alternative providers exhausted")
                else:
                    self._record_step(result, "NO_PROVIDERS_FOUND", "DiscoveryAgent", "No matching providers available")
                return

            # 2. Collect Provider Offers
            self._record_step(result, "REQUESTING_OFFERS", "ReservationWorkflow", f"Querying {len(active_candidates)} candidate(s)")
            collected_offers: list[ProviderOffer] = []

            for cand in active_candidates:
                p_id = cand.get("provider_id") or cand.get("id")
                p_agent = None
                for key_variant in (f"ProviderAgent_{p_id}", p_id, "ProviderAgent"):
                    try:
                        p_agent = self.registry.get(key_variant)
                        break
                    except KeyError:
                        continue

                if not p_agent:
                    continue

                p_msg = AgentMessage(
                    workflow_id=wf_id,
                    sender="ReservationWorkflow",
                    receiver=p_agent.name,
                    message_type="REQUEST_RESOURCE",
                    payload={"resource_request": request.model_dump()},
                )
                p_res = await p_agent.execute(p_msg)

                if p_res.status == AgentStatus.COMPLETED and p_res.data.get("success") and p_res.data.get("offer"):
                    collected_offers.append(ProviderOffer.model_validate(p_res.data["offer"]))
                else:
                    logger.info("Provider '%s' returned no viable offer; marking failed", p_id)
                    failed_providers.add(p_id)

            result.provider_offers = collected_offers
            if not collected_offers:
                recovery_res = await self._invoke_recovery(
                    result=result,
                    error="All candidate providers declined or returned zero capacity",
                    classification=FailureClassification.PROVIDER_REJECTION,
                    failed_provider=list(failed_providers)[-1] if failed_providers else None,
                    attempt=attempt,
                )
                if recovery_res.get("escalated"):
                    self._record_step(result, "ESCALATED", "RecoveryAgent", recovery_res.get("explanation"))
                    return
                continue

            # 3. Negotiation
            self._record_step(result, "NEGOTIATING", "NegotiationAgent", f"Ranking and evaluating {len(collected_offers)} offer(s)")
            neg_agent = self.registry.get("NegotiationAgent")

            neg_msg = AgentMessage(
                workflow_id=wf_id,
                sender="ReservationWorkflow",
                receiver="NegotiationAgent",
                message_type="NEGOTIATE_OFFERS",
                payload={
                    "resource_request": request.model_dump(),
                    "offers": [o.model_dump() for o in collected_offers],
                    "history": [],
                },
            )
            neg_res = await neg_agent.execute(neg_msg)

            proposal_raw = neg_res.data.get("proposal")
            if not proposal_raw:
                recovery_res = await self._invoke_recovery(
                    result=result,
                    error="Negotiation policy rejected all available candidate offers",
                    classification=FailureClassification.MISSED_DEADLINE,
                    failed_provider=collected_offers[0].provider_id,
                    attempt=attempt,
                )
                if recovery_res.get("escalated"):
                    self._record_step(result, "ESCALATED", "RecoveryAgent", recovery_res.get("explanation"))
                    return
                continue

            proposal = NegotiationProposal.model_validate(proposal_raw)
            selected_offer = ProviderOffer.model_validate(neg_res.data["selected_offer"])
            result.selected_proposal = proposal
            result.selected_offer = selected_offer

            # 4. Independent Verification
            self._record_step(result, "VERIFYING", "VerificationAgent", f"Verifying provider '{selected_offer.provider_id}'")
            ver_agent = self.registry.get("VerificationAgent")

            ver_msg = AgentMessage(
                workflow_id=wf_id,
                sender="ReservationWorkflow",
                receiver="VerificationAgent",
                message_type="VERIFY_OFFER",
                payload={
                    "resource_request": request.model_dump(),
                    "provider_offer": selected_offer.model_dump(),
                },
            )
            ver_res = await ver_agent.execute(ver_msg)

            v_raw = ver_res.data.get("verification")
            if not v_raw:
                result.error = "Verification returned no verification record"
                self._record_step(result, "FAILED", "VerificationAgent", result.error)
                return

            verification = VerificationResult.model_validate(v_raw)
            result.verification = verification

            if verification.status == VerificationStatus.VERIFIED:
                # Successfully verified! Pause for institutional approval
                result.is_ready_for_approval = True
                self._record_step(
                    result,
                    "WAITING_FOR_APPROVAL",
                    "ReservationWorkflow",
                    f"Offer from {selected_offer.provider_name} verified. Awaiting institutional human approval.",
                )
                return

            # Verification Failed: Exclude provider and attempt recovery
            failed_p = selected_offer.provider_id
            failed_providers.add(failed_p)
            reasons = "; ".join(f"{fc.check_name}: {fc.message}" for fc in verification.failed_checks)

            self._record_step(result, "VERIFICATION_FAILED", "VerificationAgent", f"Provider {failed_p} rejected: {reasons}")

            recovery_res = await self._invoke_recovery(
                result=result,
                error=f"Verification rejected provider '{failed_p}': {reasons}",
                classification=FailureClassification.PROVIDER_REJECTION,
                failed_provider=failed_p,
                attempt=attempt,
            )

            if recovery_res.get("escalated") or not recovery_res.get("is_recoverable"):
                self._record_step(result, "ESCALATED", "RecoveryAgent", recovery_res.get("explanation"))
                return

        # Exhausted attempts
        self._record_step(result, "ESCALATED", "RecoveryAgent", "Exceeded max recovery attempts. Manual intervention required.")

    async def authorize_and_commit(
        self,
        workflow_id: UUID,
        approved_by: str,
        operation: str = "LOCK_AND_COMMIT",
    ) -> ReservationWorkflowResult:
        """
        Record attributable human institutional approval and execute on-chain
        capacity locking and commitment via BlockchainExecutionAgent and MonitoringAgent.
        """
        cleaned_approver = approved_by.strip() if approved_by else ""
        if not cleaned_approver:
            raise ValueError("approved_by cannot be empty or whitespace only")

        result = self._workflows.get(workflow_id)
        if not result:
            raise KeyError(f"Workflow '{workflow_id}' not found")

        # Invariant: Prevent execution before approval and prevent duplicate execution
        if result.is_committed:
            raise ValueError(f"Workflow '{workflow_id}' has already been committed to the blockchain")

        if result.state != "WAITING_FOR_APPROVAL" or not result.is_ready_for_approval:
            raise PermissionError(
                f"Governance violation: cannot execute commitment for workflow '{workflow_id}' "
                f"in state '{result.state}'. Attributable institutional approval is required."
            )

        now = datetime.now(timezone.utc)
        result.approved_by = cleaned_approver
        result.approved_at = now

        if result.context:
            result.context.record_approval(cleaned_approver, now)

        self._record_step(
            result,
            "APPROVED",
            "InstitutionalOfficer",
            f"Authorized by {cleaned_approver}",
        )

        # -------------------------------------------------------------
        # Stage 7: On-Chain Commitment (BlockchainExecutionAgent)
        # -------------------------------------------------------------
        self._record_step(result, "COMMITTING_ONCHAIN", "BlockchainExecutionAgent", f"Executing {operation} on Coral Testnet")
        bc_agent = self.registry.get("BlockchainExecutionAgent")

        bc_msg = AgentMessage(
            workflow_id=workflow_id,
            sender="ReservationWorkflow",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload={
                "verification": result.verification.model_dump() if result.verification else {},
                "approved": True,
                "approved_by": cleaned_approver,
                "approved_at": now.isoformat(),
                "requester_id": result.request.requester_institution_id,
                "provider_id": result.selected_offer.provider_id,
                "resource_type": result.selected_offer.resource_type.value if hasattr(result.selected_offer.resource_type, "value") else str(result.selected_offer.resource_type),
                "quantity": result.selected_offer.offered_quantity,
                "operation": operation,
            },
        )

        bc_res = await bc_agent.execute(bc_msg)
        if bc_res.status == AgentStatus.FAILED:
            err = bc_res.error.message if bc_res.error else "Blockchain commitment execution failed"
            result.error = err
            self._record_step(result, "FAILED", "BlockchainExecutionAgent", err)

            # Invoke recovery for transaction revert
            await self._invoke_recovery(
                result=result,
                error=err,
                classification=FailureClassification.TRANSACTION_REVERT,
                failed_provider=result.selected_offer.provider_id,
                attempt=1,
            )
            return result

        result.onchain_result = bc_res.data
        result.is_committed = True
        tx_hash = bc_res.data.get("tx_hash")
        block_num = bc_res.data.get("block_number")

        # -------------------------------------------------------------
        # Stage 8: Confirmation & Monitoring (MonitoringAgent)
        # -------------------------------------------------------------
        self._record_step(
            result,
            "MONITORING_CONFIRMATION",
            "MonitoringAgent",
            f"Checking receipt for tx {tx_hash}",
        )
        mon_agent = self.registry.get("MonitoringAgent")

        mon_msg = AgentMessage(
            workflow_id=workflow_id,
            sender="ReservationWorkflow",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={
                "tx_hash": tx_hash,
                "submitted_at": (now - timedelta(minutes=5)).isoformat(),  # Track elapsed confirmation
            },
        )
        mon_res = await mon_agent.execute(mon_msg)

        mon_status = mon_res.data.get("status", "CONFIRMED")
        result.monitoring_status = mon_status

        if mon_res.data.get("is_delayed") or mon_status in ("DELAYED", "PENDING"):
            self._record_step(
                result,
                "DELAYED_CONFIRMATION",
                "MonitoringAgent",
                f"Transaction confirmation is delayed for {tx_hash}",
            )
            return result

        # Dispatch event to update synchronized state
        event_name = "CapacityLocked" if operation == "LOCK_CAPACITY" else "ReservationCommitted"
        await mon_agent.execute(AgentMessage(
            workflow_id=workflow_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=BlockchainEvent(
                event_name=event_name,
                transaction_hash=tx_hash,
                block_number=block_num,
                payload={
                    "reservationId": bc_res.data.get("reservation_id"),
                    "lockId": bc_res.data.get("lock_id"),
                },
            ).model_dump(),
        ))

        # Workflow reaches final confirmed state
        self._record_step(
            result,
            "CONFIRMED",
            "MonitoringAgent",
            f"On-chain commitment confirmed in block {block_num} (tx: {tx_hash})",
        )
        return result

    async def _invoke_recovery(
        self,
        result: ReservationWorkflowResult,
        error: str,
        classification: FailureClassification,
        failed_provider: Optional[str],
        attempt: int,
    ) -> dict[str, Any]:
        """Delegate diagnosis and alternative discovery to RecoveryAgent."""
        rec_agent = self.registry.get("RecoveryAgent")
        msg = AgentMessage(
            workflow_id=result.workflow_id,
            sender="ReservationWorkflow",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": error,
                "failure_classification": classification.value,
                "failed_provider_id": failed_provider,
                "resource_request": result.request.model_dump() if result.request else None,
                "attempt_number": attempt,
                "failure_history": result.recovery_attempts,
            },
        )
        rec_res = await rec_agent.execute(msg)
        result.recovery_attempts.append({
            "attempt": attempt,
            "classification": classification.value,
            "error": error,
            "strategy": rec_res.data.get("selected_strategy"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return rec_res.data

    def _record_step(
        self,
        result: ReservationWorkflowResult,
        new_state: str,
        agent_name: str,
        details: str,
    ) -> None:
        """Record an immutable chronological audit trail step."""
        now = datetime.now(timezone.utc)
        step = {
            "from_state": result.state,
            "to_state": new_state,
            "agent": agent_name,
            "timestamp": now.isoformat(),
            "details": details,
        }
        result.audit_trail.append(step)
        result.state = new_state
        result.updated_at = now
        logger.info(
            "Workflow %s: %s -> %s (by %s): %s",
            result.workflow_id,
            step["from_state"],
            new_state,
            agent_name,
            details,
        )
