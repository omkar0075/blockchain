from datetime import datetime, timezone
from enum import Enum
import logging
from typing import Any, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

from .base import BaseAgent
from .discovery_agent import DiscoveryAgent
from .negotiation_agent import NegotiationAgent
from .registry import AgentRegistry, default_registry
from .verification_agent import VerificationAgent
from ..schemas.agent_messages import (
    AgentError,
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from ..schemas.provider_offer import ProviderOffer
from ..schemas.resource_request import ResourceConstraints, ResourceRequest

logger = logging.getLogger("medway.agents.recovery")


class FailureClassification(str, Enum):
    """Categorization of workflow, provider, and on-chain execution failures."""
    PROVIDER_REJECTION = "PROVIDER_REJECTION"
    INSUFFICIENT_CAPACITY = "INSUFFICIENT_CAPACITY"
    EXPIRED_LOCK = "EXPIRED_LOCK"
    TRANSACTION_REVERT = "TRANSACTION_REVERT"
    RPC_FAILURE = "RPC_FAILURE"
    MISSED_DEADLINE = "MISSED_DEADLINE"
    PROVIDER_NON_FULFILLMENT = "PROVIDER_NON_FULFILLMENT"
    SECURITY_OR_POLICY_VIOLATION = "SECURITY_OR_POLICY_VIOLATION"
    UNKNOWN = "UNKNOWN"


class RecoveryStrategy(str, Enum):
    """Actionable remediation path chosen by the Recovery Agent."""
    FIND_ALTERNATIVE_PROVIDER = "FIND_ALTERNATIVE_PROVIDER"
    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    RE_NEGOTIATE = "RE_NEGOTIATE"
    RELEASE_LOCK_AND_RETRY = "RELEASE_LOCK_AND_RETRY"
    INITIATE_DISPUTE = "INITIATE_DISPUTE"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    ABORT_UNRECOVERABLE = "ABORT_UNRECOVERABLE"


class RecoveryOutcome(BaseModel):
    """Structured recovery diagnosis and remediation decision."""
    model_config = ConfigDict(extra="ignore")

    workflow_id: UUID
    failure_classification: FailureClassification
    is_recoverable: bool
    selected_strategy: RecoveryStrategy
    attempt_number: int = 1
    max_attempts: int = 3
    failed_provider_id: Optional[str] = None
    alternative_provider_id: Optional[str] = None
    alternative_offers: list[ProviderOffer] = Field(default_factory=list)
    requires_fresh_verification: bool = True
    requires_human_approval: bool = True
    released_lock_id: Optional[str] = None
    failure_history: list[dict[str, Any]] = Field(default_factory=list)
    explanation: str
    escalated: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RecoveryAgent(BaseAgent):
    """
    Autonomous Recovery Agent.

    Diagnoses coordination failures, classifies root causes, prevents infinite retries,
    safely discovers alternative providers, re-enters negotiation, and enforces fresh verification.

    Architectural & Governance Invariants:
    1. Informed Remediation: Recovery does not blindly repeat failed operations; it classifies
       failure root causes and chooses appropriate strategies.
    2. Zero Secret Compromise: Preserves security boundaries; security violations are unrecoverable.
    3. Fresh Verification & Approval Gating: Alternatives MUST undergo fresh independent verification
       and institutional authorization before any blockchain execution.
    4. Never Silently Cancel Commitments: Committed reservations require formal dispute or
       attributable authorization, never silent off-chain deletion.
    5. Bounded Retry Protection: Strictly caps attempts per workflow (max_recovery_attempts)
       and escalates to human review when exhausted.
    6. Audit Lineage: Retains the complete history of previous failures and attempts.
    """

    def __init__(
        self,
        name: str = "RecoveryAgent",
        registry: Optional[AgentRegistry] = None,
        max_recovery_attempts: int = 3,
        backoff_base_seconds: float = 2.0,
        advisor: Optional[Any] = None,
    ) -> None:
        super().__init__(name=name)
        self.registry = registry if registry is not None else default_registry
        self.max_recovery_attempts = max_recovery_attempts
        self.backoff_base_seconds = backoff_base_seconds
        self.advisor = advisor
        self._workflow_attempt_counts: dict[UUID, int] = {}
        self._workflow_failure_logs: dict[UUID, list[dict[str, Any]]] = {}

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Diagnose a failed coordination step and determine an actionable recovery path.

        Expected payload keys:
            - error (str | dict): Error details or message.
            - failure_classification (str, optional): Pre-classified failure type.
            - resource_request (dict | ResourceRequest): Original request.
            - failed_provider_id (str, optional): ID of provider that failed.
            - lock_id (str, optional): Expired or abandoned capacity lock ID.
            - commitment_status (str, optional): e.g. 'COMMITTED', 'LOCKED', 'OFFERED'.
            - attempt_number (int, optional): Current attempt index.
            - failure_history (list, optional): Previous failure audit log.
        """
        payload = message.payload or {}
        wf_id = message.workflow_id
        now = datetime.now(timezone.utc)

        # 1. Track & Bounded Attempts
        current_attempt = payload.get("attempt_number") or self._workflow_attempt_counts.get(wf_id, 0) + 1
        self._workflow_attempt_counts[wf_id] = current_attempt

        # Retrieve failure history
        history: list[dict[str, Any]] = list(payload.get("failure_history") or self._workflow_failure_logs.get(wf_id, []))

        # 2. Classify Failure
        classification = self._classify_failure(payload)
        failed_provider_id = (
            payload.get("failed_provider_id")
            or payload.get("provider_id")
            or (payload.get("selected_offer", {}).get("provider_id") if isinstance(payload.get("selected_offer"), dict) else None)
        )

        # Log this failure event
        failure_entry = {
            "attempt": current_attempt,
            "classification": classification.value,
            "error": str(payload.get("error", "Unknown")),
            "failed_provider_id": failed_provider_id,
            "timestamp": now.isoformat(),
        }
        history.append(failure_entry)
        self._workflow_failure_logs[wf_id] = history

        # Consult optional LLM recovery advisor for intelligent root-cause diagnosis
        recovery_advice = None
        if self.advisor is not None:
            try:
                if hasattr(self.advisor, "advise_async"):
                    res = await self.advisor.advise_async(payload)
                elif hasattr(self.advisor, "advise"):
                    res = self.advisor.advise(payload)
                else:
                    res = None
                if res:
                    recovery_advice = res.model_dump() if hasattr(res, "model_dump") else res
            except Exception as exc:
                logger.warning("LLM recovery advisor evaluation failed: %s", exc)

        # 3. Check for Committed Reservation Invariant (Never silently cancel)
        commitment_status = str(payload.get("commitment_status", "")).upper()
        if commitment_status == "COMMITTED" or classification == FailureClassification.PROVIDER_NON_FULFILLMENT:
            # Committed reservation cannot be cancelled silently. Must initiate dispute or escalate.
            result = self._handle_committed_failure(wf_id, classification, failed_provider_id, history, payload)
            if recovery_advice and isinstance(result, dict):
                result["recovery_advice"] = recovery_advice
            return result

        # 4. Check Retry Budget
        if current_attempt > self.max_recovery_attempts:
            logger.warning("Workflow %s exceeded max recovery attempts (%d)", wf_id, self.max_recovery_attempts)
            result = self._escalate_to_human(
                wf_id=wf_id,
                classification=classification,
                history=history,
                failed_provider_id=failed_provider_id,
                reason=f"Exceeded maximum recovery attempts ({self.max_recovery_attempts}). Manual intervention required.",
            )
            if recovery_advice and isinstance(result, dict):
                result["recovery_advice"] = recovery_advice
            return result

        # 5. Determine Recoverability & Strategy
        is_recoverable, strategy = self._determine_strategy(classification, payload)

        if not is_recoverable:
            result = {
                "success": False,
                "is_recoverable": False,
                "selected_strategy": strategy.value,
                "failure_classification": classification.value,
                "workflow_id": str(wf_id),
                "attempt_number": current_attempt,
                "failure_history": history,
                "requires_fresh_verification": True,
                "requires_human_approval": True,
                "escalated": True,
                "explanation": f"Failure classified as non-recoverable ({classification.value}): {payload.get('error')}",
                "message": "Coordination failure cannot be automatically recovered. Aborted safely.",
            }
            if recovery_advice and isinstance(result, dict):
                result["recovery_advice"] = recovery_advice
            return result

        # 6. Execute Remediation Strategy
        released_lock_id = None
        if strategy == RecoveryStrategy.RELEASE_LOCK_AND_RETRY:
            lock_id = payload.get("lock_id") or payload.get("released_lock_id")
            if lock_id:
                released_lock_id = str(lock_id)
                logger.info("Recorded lock release for expired/abandoned lock %s", released_lock_id)
            strategy = RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER

        if strategy == RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER:
            result = await self._find_alternative_provider(
                wf_id=wf_id,
                classification=classification,
                failed_provider_id=failed_provider_id,
                current_attempt=current_attempt,
                history=history,
                payload=payload,
                released_lock_id=released_lock_id,
            )
            if recovery_advice and isinstance(result, dict):
                result["recovery_advice"] = recovery_advice
            return result

        elif strategy == RecoveryStrategy.RETRY_WITH_BACKOFF:
            backoff_delay = self.backoff_base_seconds * (2 ** (current_attempt - 1))
            result = {
                "success": True,
                "is_recoverable": True,
                "selected_strategy": strategy.value,
                "failure_classification": classification.value,
                "workflow_id": str(wf_id),
                "attempt_number": current_attempt,
                "backoff_seconds": backoff_delay,
                "failure_history": history,
                "requires_fresh_verification": True,
                "requires_human_approval": True,
                "escalated": False,
                "explanation": f"Transient failure ({classification.value}). Scheduled retry with {backoff_delay}s backoff.",
                "message": f"Retrying transient failure with {backoff_delay}s exponential backoff.",
            }
            if recovery_advice and isinstance(result, dict):
                result["recovery_advice"] = recovery_advice
            return result

        # Fallback escalation
        result = self._escalate_to_human(
            wf_id=wf_id,
            classification=classification,
            history=history,
            failed_provider_id=failed_provider_id,
            reason="No automated recovery strategy could be applied.",
        )
        if recovery_advice and isinstance(result, dict):
            result["recovery_advice"] = recovery_advice
        return result

    def _classify_failure(self, payload: dict[str, Any]) -> FailureClassification:
        """Classify the root cause of an error based on structured reason codes or error text."""
        explicit = payload.get("failure_classification") or payload.get("reason_code")
        if explicit:
            for member in FailureClassification:
                if member.value.lower() == str(explicit).lower():
                    return member

        err_str = str(payload.get("error", "")).lower()

        if "revert" in err_str:
            return FailureClassification.TRANSACTION_REVERT
        if "timeout" in err_str or "rpc" in err_str or "connection" in err_str or "unreachable" in err_str:
            return FailureClassification.RPC_FAILURE
        if "expired" in err_str and "lock" in err_str:
            return FailureClassification.EXPIRED_LOCK
        if "insufficient" in err_str or "capacity" in err_str or "no available slot" in err_str:
            return FailureClassification.INSUFFICIENT_CAPACITY
        if "privacy" in err_str or "security" in err_str or "forbidden" in err_str or "unauthorized" in err_str:
            return FailureClassification.SECURITY_OR_POLICY_VIOLATION
        if "non-fulfillment" in err_str or "delivery missed" in err_str:
            return FailureClassification.PROVIDER_NON_FULFILLMENT
        if "deadline" in err_str or "sla" in err_str:
            return FailureClassification.MISSED_DEADLINE
        if "rejected" in err_str or "declined" in err_str or "incompatible" in err_str:
            return FailureClassification.PROVIDER_REJECTION

        return FailureClassification.UNKNOWN

    def _determine_strategy(
        self,
        classification: FailureClassification,
        payload: dict[str, Any],
    ) -> tuple[bool, RecoveryStrategy]:
        """Determine whether a classified failure is recoverable and select its strategy."""
        if classification == FailureClassification.SECURITY_OR_POLICY_VIOLATION:
            return False, RecoveryStrategy.ABORT_UNRECOVERABLE

        if classification in (
            FailureClassification.PROVIDER_REJECTION,
            FailureClassification.INSUFFICIENT_CAPACITY,
            FailureClassification.MISSED_DEADLINE,
        ):
            return True, RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER

        if classification == FailureClassification.EXPIRED_LOCK:
            return True, RecoveryStrategy.RELEASE_LOCK_AND_RETRY

        if classification == FailureClassification.RPC_FAILURE:
            return True, RecoveryStrategy.RETRY_WITH_BACKOFF

        if classification == FailureClassification.TRANSACTION_REVERT:
            # Capacity race condition on-chain -> switch to alternative provider
            return True, RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER

        if classification == FailureClassification.PROVIDER_NON_FULFILLMENT:
            return True, RecoveryStrategy.INITIATE_DISPUTE

        return False, RecoveryStrategy.ESCALATE_TO_HUMAN

    async def _find_alternative_provider(
        self,
        wf_id: UUID,
        classification: FailureClassification,
        failed_provider_id: Optional[str],
        current_attempt: int,
        history: list[dict[str, Any]],
        payload: dict[str, Any],
        released_lock_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Search for alternative providers, excluding the failed provider."""
        req_raw = payload.get("resource_request") or payload.get("request")
        if not req_raw:
            return self._escalate_to_human(
                wf_id=wf_id,
                classification=classification,
                history=history,
                failed_provider_id=failed_provider_id,
                reason="Cannot find alternative providers: original ResourceRequest missing from recovery payload.",
            )

        request = ResourceRequest.model_validate(req_raw)

        # Build excluded providers set
        current_excluded = set(request.constraints.excluded_providers) if request.constraints else set()
        if failed_provider_id:
            current_excluded.add(failed_provider_id)

        # Add any previously failed providers from history
        for entry in history:
            p = entry.get("failed_provider_id")
            if p:
                current_excluded.add(p)

        # Create updated request with excluded provider constraints
        updated_constraints = (
            request.constraints.model_copy(update={"excluded_providers": list(current_excluded)})
            if request.constraints
            else ResourceConstraints(excluded_providers=list(current_excluded))
        )
        updated_request = request.model_copy(update={"constraints": updated_constraints})

        # Query DiscoveryAgent
        discovery_agent = None
        try:
            discovery_agent = self.registry.get("DiscoveryAgent")
        except KeyError:
            discovery_agent = DiscoveryAgent()

        disc_msg = AgentMessage(
            workflow_id=wf_id,
            sender="RecoveryAgent",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_PROVIDERS",
            payload={"resource_request": updated_request.model_dump()},
        )

        disc_res = await discovery_agent.execute(disc_msg)
        candidates = (
            disc_res.data.get("candidate_offers")
            or disc_res.data.get("candidates")
            or []
        )

        if not candidates or not disc_res.data.get("success"):
            return self._escalate_to_human(
                wf_id=wf_id,
                classification=classification,
                history=history,
                failed_provider_id=failed_provider_id,
                reason=f"No alternative providers found after excluding: {', '.join(sorted(current_excluded))}.",
            )

        alt_candidate = candidates[0]
        alt_provider_id = alt_candidate.get("provider_id") or alt_candidate.get("id")

        return {
            "success": True,
            "is_recoverable": True,
            "selected_strategy": RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER.value,
            "failure_classification": classification.value,
            "workflow_id": str(wf_id),
            "attempt_number": current_attempt,
            "failed_provider_id": failed_provider_id,
            "alternative_provider_id": alt_provider_id,
            "alternative_offers": candidates,
            "released_lock_id": released_lock_id,
            # Invariant 3: Fresh verification & authorization strictly required
            "requires_fresh_verification": True,
            "requires_human_approval": True,
            "ready_for_blockchain": False,
            "failure_history": history,
            "escalated": False,
            "explanation": (
                f"Recovered from {classification.value} by excluding failed provider '{failed_provider_id}'. "
                f"Discovered {len(candidates)} alternative candidate(s). Next candidate: '{alt_provider_id}'. "
                "Fresh verification and institutional authorization required before commitment."
            ),
            "message": f"Alternative provider '{alt_provider_id}' identified. Fresh verification and authorization required.",
        }

    def _handle_committed_failure(
        self,
        wf_id: UUID,
        classification: FailureClassification,
        failed_provider_id: Optional[str],
        history: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Enforces Invariant 4: Never silently cancel a committed reservation."""
        reservation_id = payload.get("reservation_id")
        return {
            "success": True,
            "is_recoverable": True,
            "selected_strategy": RecoveryStrategy.INITIATE_DISPUTE.value,
            "failure_classification": classification.value,
            "workflow_id": str(wf_id),
            "reservation_id": reservation_id,
            "failed_provider_id": failed_provider_id,
            "failure_history": history,
            "requires_fresh_verification": True,
            "requires_human_approval": True,
            "escalated": True,
            "explanation": (
                f"Committed reservation '{reservation_id}' failed due to {classification.value}. "
                "Invariant enforced: cannot silently cancel an active on-chain commitment. "
                "Consortium arbitration dispute initiated and escalated for administrative review."
            ),
            "message": "On-chain commitment failure escalated to Consortium Arbitration dispute.",
        }

    def _escalate_to_human(
        self,
        wf_id: UUID,
        classification: FailureClassification,
        history: list[dict[str, Any]],
        failed_provider_id: Optional[str],
        reason: str,
    ) -> dict[str, Any]:
        """Escalate unresolvable failures for institutional human review."""
        return {
            "success": True,
            "is_recoverable": False,
            "selected_strategy": RecoveryStrategy.ESCALATE_TO_HUMAN.value,
            "failure_classification": classification.value,
            "workflow_id": str(wf_id),
            "failed_provider_id": failed_provider_id,
            "failure_history": history,
            "requires_fresh_verification": True,
            "requires_human_approval": True,
            "escalated": True,
            "explanation": f"Workflow escalated for human review: {reason}",
            "message": f"Escalated to Institutional Officer: {reason}",
        }

