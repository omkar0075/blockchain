import asyncio
import concurrent.futures
import json
import logging
from typing import Any, Optional
from uuid import UUID

from ..agents.recovery_agent import FailureClassification, RecoveryStrategy
from .client import LLMClient
from .guardrails import LLMGuardrailViolation, LLMOutputValidationError
from .schemas import LLMError, RecoveryAdviceOutput

logger = logging.getLogger("medway.llm.recovery")

RECOVERY_ADVISOR_SYSTEM_PROMPT = """You are MEDway's Healthcare Workflow Recovery Advisor.
Your mission is to analyze coordination and execution failures across hospitals, diagnostic labs, and smart contract reservations, explain the root cause, and recommend safe, structured recovery strategies.

Failure Classifications:
- PROVIDER_REJECTION: Provider explicitly declined or is incompatible.
- INSUFFICIENT_CAPACITY: Provider lacks requested quantity or slots.
- TRANSACTION_REVERT: On-chain capacity lock or commitment transaction reverted (e.g. capacity race).
- RPC_FAILURE: Network disconnection, RPC endpoint error, or timeout.
- EXPIRED_LOCK: 2PC capacity reservation lock expired before confirmation.
- MISSED_DEADLINE: Provider cannot satisfy the required delivery deadline.
- PROVIDER_NON_FULFILLMENT: Provider failed to deliver committed resource after confirmation.
- SECURITY_OR_POLICY_VIOLATION: Input or operation violated privacy, security, or authentication policy.
- UNKNOWN: Ambiguous or unclassified failure.

Recovery Strategies:
- FIND_ALTERNATIVE_PROVIDER: Search for accredited alternative providers (excluding the failed provider).
- RE_NEGOTIATE: Suggest renegotiation for partial quantity or altered SLA.
- RETRY_WITH_BACKOFF: Transient network/RPC retry with exponential backoff.
- RELEASE_LOCK_AND_RETRY: Release stale on-chain lock and re-attempt capacity lock.
- INITIATE_DISPUTE: Formal on-chain clinical dispute for non-fulfillment.
- ESCALATE_TO_HUMAN: Escalate to human institutional officer when attempts are exhausted or manual review is required.
- ABORT_UNRECOVERABLE: Immediately abort workflow when security, privacy, or immutable policy is violated.

Governance & Safety Rules:
1. Security and privacy violations are strictly UNRECOVERABLE (is_recoverable=false, requires_human_intervention=true).
2. When attempts meet or exceed max_attempts, MUST escalate to human (ESCALATE_TO_HUMAN, is_recoverable=false).
3. NEVER suggest the failed provider as an alternative provider.
4. Advisory role only: You recommend strategies; you NEVER execute contract operations or reserve capacity.

Output must strictly conform to RecoveryAdviceOutput."""


class LLMRecoveryAdvisor:
    """
    LLM-Powered Healthcare Workflow Recovery Advisor.

    Diagnoses coordination and on-chain execution failures, classifies root causes,
    and recommends remediation strategies with deterministic constraint enforcement.

    Architectural Invariants:
    1. Advisory Role Only: Explains and recommends recovery strategies, but NEVER executes
       blockchain transactions, modifies locks, or overrides verification.
    2. Deterministic Verification: Security policies, bounded retry exhaustion, and provider
       exclusion are enforced deterministically.
    3. Deterministic Fallback: When the LLM is unavailable or hallucinates invalid strategies,
       falls back immediately to rule-based diagnosis and remediation.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        enable_fallback: bool = True,
    ) -> None:
        self.client = llm_client or LLMClient()
        self.enable_fallback = enable_fallback

    async def advise_async(self, failure_payload: dict[str, Any]) -> RecoveryAdviceOutput:
        """
        Asynchronously diagnose a workflow failure and generate recovery advice.
        """
        err_text = str(failure_payload.get("error", "Unknown workflow error"))
        attempt = int(failure_payload.get("attempt", 1))
        max_attempts = int(failure_payload.get("max_attempts", 3))
        failed_p_id = failure_payload.get("failed_provider", failure_payload.get("failed_provider_id"))

        # Pre-check: Deterministic non-recoverable overrides
        if attempt >= max_attempts:
            return self._build_exhausted_retries_advice(attempt, max_attempts, failed_p_id)

        if self._is_security_violation(err_text):
            return self._build_security_violation_advice(err_text)

        # Build prompt for LLM diagnosis
        prompt = self._build_diagnosis_prompt(failure_payload)

        try:
            raw_advice: RecoveryAdviceOutput = await self.client.generate_structured(
                prompt=prompt,
                schema=RecoveryAdviceOutput,
                system_prompt=RECOVERY_ADVISOR_SYSTEM_PROMPT,
            )

            # Deterministic sanitization and validation
            validated_advice = self._validate_and_sanitize_advice(
                advice=raw_advice,
                failure_payload=failure_payload,
            )
            return validated_advice

        except (LLMError, LLMOutputValidationError, LLMGuardrailViolation) as exc:
            if not self.enable_fallback:
                raise
            logger.warning("LLM recovery advisor unavailable (%s); using deterministic diagnosis", exc)
            return self._diagnose_deterministically(failure_payload)
        except Exception as exc:
            if not self.enable_fallback:
                raise
            logger.error("Unexpected error in LLM recovery advisor (%s); using fallback", exc)
            return self._diagnose_deterministically(failure_payload)

    def advise(self, failure_payload: dict[str, Any]) -> RecoveryAdviceOutput:
        """Synchronous wrapper for advise_async."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run,
                    self.advise_async(failure_payload),
                ).result(timeout=15.0)
        else:
            return asyncio.run(self.advise_async(failure_payload))

    def _build_diagnosis_prompt(self, payload: dict[str, Any]) -> str:
        """Construct a structured diagnosis prompt for the model."""
        lines = [
            f"Error Details: {payload.get('error', 'Unspecified error')}",
            f"Failed Stage/Agent: {payload.get('failed_stage', payload.get('agent', 'Unknown'))}",
            f"Failed Provider ID: {payload.get('failed_provider', payload.get('failed_provider_id', 'None'))}",
            f"Attempt Count: {payload.get('attempt', 1)} of {payload.get('max_attempts', 3)}",
            f"Explicit Reason Code: {payload.get('failure_classification', payload.get('reason_code', 'None'))}",
            f"Known Alternative Providers: {payload.get('known_providers', ['hospital-b', 'lab-c'])}",
        ]

        req = payload.get("resource_request") or payload.get("request")
        if req:
            lines.append(f"Resource Request Context: {req}")

        if payload.get("is_dispute"):
            lines.append("Context: Provider has failed delivery following confirmed commitment.")

        return "\n".join(lines)

    def _is_security_violation(self, err_text: str) -> bool:
        """Determine if an error is a security, authentication, or privacy violation."""
        lower = err_text.lower()
        return any(
            k in lower
            for k in ("privacy", "security", "forbidden", "unauthorized", "credential", "ssn", "private_key")
        )

    def _build_security_violation_advice(self, err_text: str) -> RecoveryAdviceOutput:
        """Construct non-recoverable advice for security and privacy violations."""
        return RecoveryAdviceOutput(
            failure_classification=FailureClassification.SECURITY_OR_POLICY_VIOLATION.value,
            root_cause_explanation=f"Security/privacy violation detected: {err_text}",
            suggested_recovery_path=RecoveryStrategy.ABORT_UNRECOVERABLE.value,
            is_recoverable=False,
            requires_human_intervention=True,
            alternative_provider_ids=[],
            remediation_steps=[
                "Immediately abort automated coordination pipeline.",
                "Quarantine payload and audit trail.",
                "Escalate incident to Institutional Information Security Officer.",
            ],
        )

    def _build_exhausted_retries_advice(self, attempt: int, max_attempts: int, failed_p: Optional[str]) -> RecoveryAdviceOutput:
        """Construct escalation advice when bounded recovery attempts are exhausted."""
        return RecoveryAdviceOutput(
            failure_classification=FailureClassification.UNKNOWN.value,
            root_cause_explanation=f"Exhausted maximum allowable recovery attempts ({attempt}/{max_attempts}).",
            suggested_recovery_path=RecoveryStrategy.ESCALATE_TO_HUMAN.value,
            is_recoverable=False,
            requires_human_intervention=True,
            alternative_provider_ids=[],
            remediation_steps=[
                "Halt autonomous recovery to prevent thrashing or capacity exhaustion.",
                "Present full negotiation and failure history to human coordinator.",
                "Require explicit manual intervention before initiating any further attempts.",
            ],
        )

    def _validate_and_sanitize_advice(
        self,
        advice: RecoveryAdviceOutput,
        failure_payload: dict[str, Any],
    ) -> RecoveryAdviceOutput:
        """
        Deterministically sanitize LLM output:
        - Excludes failed provider from alternative_provider_ids.
        - Enforces escalation for disputes and exhausted attempts.
        - Enforces unrecoverability for security violations.
        """
        err_text = str(failure_payload.get("error", ""))
        failed_p = failure_payload.get("failed_provider", failure_payload.get("failed_provider_id"))
        attempt = int(failure_payload.get("attempt", 1))
        max_attempts = int(failure_payload.get("max_attempts", 3))

        # Invariant 1: Security violations cannot be recoverable
        if self._is_security_violation(err_text):
            return self._build_security_violation_advice(err_text)

        # Invariant 2: Exhausted attempts must escalate
        if attempt >= max_attempts:
            return self._build_exhausted_retries_advice(attempt, max_attempts, failed_p)

        # Invariant 3: Exclude failed provider from alternatives
        if failed_p and failed_p in advice.alternative_provider_ids:
            advice.alternative_provider_ids = [p for p in advice.alternative_provider_ids if p != failed_p]

        # Invariant 4: Disputes require human intervention
        if failure_payload.get("is_dispute") or "dispute" in err_text.lower():
            advice.suggested_recovery_path = RecoveryStrategy.INITIATE_DISPUTE.value
            advice.requires_human_intervention = True
            advice.failure_classification = FailureClassification.PROVIDER_NON_FULFILLMENT.value

        # Invariant 5: Strategy validation
        valid_strategies = {s.value for s in RecoveryStrategy}
        if advice.suggested_recovery_path not in valid_strategies:
            logger.warning("LLM proposed invalid strategy '%s'; defaulting to fallback", advice.suggested_recovery_path)
            return self._diagnose_deterministically(failure_payload)

        return advice

    def _diagnose_deterministically(self, payload: dict[str, Any]) -> RecoveryAdviceOutput:
        """
        Deterministic rule-based diagnosis and remediation fallback.
        """
        err_str = str(payload.get("error", "")).lower()
        explicit = payload.get("failure_classification", payload.get("reason_code"))
        failed_p = payload.get("failed_provider", payload.get("failed_provider_id"))
        known_providers = payload.get("known_providers", ["hospital-b", "lab-c"])
        candidates = [p for p in known_providers if p != failed_p]

        # 1. Classification
        classification = FailureClassification.UNKNOWN
        if explicit:
            for m in FailureClassification:
                if m.value.lower() == str(explicit).lower():
                    classification = m
                    break

        if classification == FailureClassification.UNKNOWN:
            if "privacy" in err_str or "security" in err_str or "forbidden" in err_str or "unauthorized" in err_str:
                classification = FailureClassification.SECURITY_OR_POLICY_VIOLATION
            elif "dispute" in err_str or "non-fulfillment" in err_str or "breach" in err_str or payload.get("is_dispute"):
                classification = FailureClassification.PROVIDER_NON_FULFILLMENT
            elif "revert" in err_str:
                classification = FailureClassification.TRANSACTION_REVERT
            elif "timeout" in err_str or "rpc" in err_str or "connection" in err_str or "unreachable" in err_str:
                classification = FailureClassification.RPC_FAILURE
            elif "expired" in err_str and "lock" in err_str:
                classification = FailureClassification.EXPIRED_LOCK
            elif "insufficient" in err_str or "capacity" in err_str or "no available slot" in err_str:
                classification = FailureClassification.INSUFFICIENT_CAPACITY
            elif "deadline" in err_str or "sla" in err_str:
                classification = FailureClassification.MISSED_DEADLINE
            elif "reject" in err_str or "decline" in err_str or "incompatible" in err_str:
                classification = FailureClassification.PROVIDER_REJECTION

        # 2. Strategy Mapping
        if classification == FailureClassification.SECURITY_OR_POLICY_VIOLATION:
            return self._build_security_violation_advice(err_str)

        if classification == FailureClassification.PROVIDER_NON_FULFILLMENT:
            return RecoveryAdviceOutput(
                failure_classification=classification.value,
                root_cause_explanation="Committed provider failed delivery SLA after blockchain confirmation.",
                suggested_recovery_path=RecoveryStrategy.INITIATE_DISPUTE.value,
                is_recoverable=True,
                requires_human_intervention=True,
                alternative_provider_ids=candidates,
                remediation_steps=[
                    "Initiate on-chain dispute transaction on smart contract.",
                    "Notify institutional compliance officer.",
                    "Seek replacement capacity from alternative providers.",
                ],
            )

        if classification == FailureClassification.RPC_FAILURE:
            return RecoveryAdviceOutput(
                failure_classification=classification.value,
                root_cause_explanation="Temporary blockchain RPC endpoint connection failure or network timeout.",
                suggested_recovery_path=RecoveryStrategy.RETRY_WITH_BACKOFF.value,
                is_recoverable=True,
                requires_human_intervention=False,
                alternative_provider_ids=[],
                remediation_steps=[
                    "Execute bounded exponential backoff.",
                    "Verify RPC node health.",
                    "Retry submission without modifying negotiation terms.",
                ],
            )

        if classification == FailureClassification.EXPIRED_LOCK:
            return RecoveryAdviceOutput(
                failure_classification=classification.value,
                root_cause_explanation="2PC capacity lock duration expired prior to institutional commitment.",
                suggested_recovery_path=RecoveryStrategy.RELEASE_LOCK_AND_RETRY.value,
                is_recoverable=True,
                requires_human_intervention=False,
                alternative_provider_ids=candidates,
                remediation_steps=[
                    "Call releaseCapacityLock on smart contract to free reserved quota.",
                    "Obtain fresh offer and capacity verification.",
                    "Re-submit capacity lock with refreshed TTL.",
                ],
            )

        if classification == FailureClassification.INSUFFICIENT_CAPACITY and ("partial" in err_str or "counteroffer" in err_str):
            partial_qty = payload.get("partial_quantity", 2)
            return RecoveryAdviceOutput(
                failure_classification=classification.value,
                root_cause_explanation=f"Provider {failed_p} cannot fulfill complete volume but offers partial slots.",
                suggested_recovery_path=RecoveryStrategy.RE_NEGOTIATE.value,
                is_recoverable=True,
                requires_human_intervention=False,
                alternative_provider_ids=candidates,
                suggested_counteroffer_quantity=partial_qty,
                remediation_steps=[
                    f"Formulate counteroffer for partial quantity ({partial_qty}).",
                    "Seek requester institutional approval for split/partial fulfillment.",
                ],
            )

        # Provider rejection, capacity race revert, or general capacity shortage
        return RecoveryAdviceOutput(
            failure_classification=classification.value,
            root_cause_explanation=(
                f"Provider {failed_p or 'candidate'} encountered {classification.value.lower()}: {err_str}."
            ),
            suggested_recovery_path=RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER.value,
            is_recoverable=True,
            requires_human_intervention=False,
            alternative_provider_ids=candidates,
            remediation_steps=[
                f"Exclude failed provider '{failed_p}' from candidate registry.",
                "Execute fresh discovery query for alternative accredited providers.",
                "Collect and negotiate offers from remaining candidates.",
                "Enforce fresh accreditation verification and attributable human approval.",
            ],
        )
