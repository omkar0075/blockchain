import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4
import pytest

from app.agents.recovery_agent import (
    FailureClassification,
    RecoveryAgent,
    RecoveryStrategy,
)
from app.llm.client import LLMClient
from app.llm.provider import MockLLMProvider
from app.llm.recovery_advisor import LLMRecoveryAdvisor
from app.llm.schemas import (
    LLMError,
    LLMOutputValidationError,
    LLMTimeoutError,
    RecoveryAdviceOutput,
)
from app.schemas.agent_messages import AgentMessage, AgentStatus
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)


def create_sample_request(quantity: int = 3) -> ResourceRequest:
    return ResourceRequest(
        request_id=uuid4(),
        requester_institution_id="hospital-a",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=quantity,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city="Metropolis"),
        required_by=datetime.now(timezone.utc),
        required_by_hours=12,
        constraints=ResourceConstraints(excluded_providers=[]),
    )


# -----------------------------------------------------------------------------
# Test 1: Identifies provider rejection
# -----------------------------------------------------------------------------
def test_identifies_provider_rejection():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "failure_classification": "PROVIDER_REJECTION",
                    "root_cause_explanation": "Provider Hospital-B explicitly declined the offer due to schedule conflict.",
                    "suggested_recovery_path": "FIND_ALTERNATIVE_PROVIDER",
                    "is_recoverable": True,
                    "requires_human_intervention": False,
                    "alternative_provider_ids": ["lab-c", "hospital-d"],
                    "suggested_counteroffer_quantity": None,
                    "remediation_steps": [
                        "Exclude Hospital-B from provider candidate pool.",
                        "Query Discovery Agent for alternative accredited diagnostic slot providers.",
                        "Initiate fresh negotiation round.",
                    ],
                })
            ]
        )

        advisor = LLMRecoveryAdvisor(llm_client=LLMClient(provider=mock_provider))
        payload = {
            "error": "Provider Hospital-B explicitly declined: conflicting scheduled maintenance",
            "failed_provider": "hospital-b",
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["lab-c", "hospital-d"],
        }
        advice = await advisor.advise_async(payload)

        assert advice.failure_classification == "PROVIDER_REJECTION"
        assert advice.suggested_recovery_path == "FIND_ALTERNATIVE_PROVIDER"
        assert advice.is_recoverable is True
        assert advice.requires_human_intervention is False
        assert "hospital-b" not in advice.alternative_provider_ids
        assert len(advice.alternative_provider_ids) > 0
        assert len(advice.remediation_steps) >= 2

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 2: Identifies insufficient capacity
# -----------------------------------------------------------------------------
def test_identifies_insufficient_capacity():
    async def run():
        advisor = LLMRecoveryAdvisor()  # Uses deterministic fallback engine
        payload = {
            "error": "Insufficient diagnostic slots: requested 5, available 0 at this facility",
            "failed_provider": "hospital-b",
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["hospital-b", "lab-c", "lab-d"],
        }
        advice = await advisor.advise_async(payload)

        assert advice.failure_classification == FailureClassification.INSUFFICIENT_CAPACITY.value
        assert advice.is_recoverable is True
        assert advice.suggested_recovery_path == RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER.value
        assert "hospital-b" not in advice.alternative_provider_ids
        assert "lab-c" in advice.alternative_provider_ids

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 3: Identifies transaction failure
# -----------------------------------------------------------------------------
def test_identifies_transaction_failure():
    async def run():
        advisor = LLMRecoveryAdvisor()
        payload = {
            "error": "Transaction reverted on chain: execution reverted by EVM (CapacityRaceRevert)",
            "failed_provider": "hospital-b",
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["hospital-b", "lab-c"],
        }
        advice = await advisor.advise_async(payload)

        assert advice.failure_classification == FailureClassification.TRANSACTION_REVERT.value
        assert advice.is_recoverable is True
        assert advice.suggested_recovery_path == RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER.value
        assert "hospital-b" not in advice.alternative_provider_ids

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 4: Recommends alternative providers (strictly excluding failed provider)
# -----------------------------------------------------------------------------
def test_recommends_alternative_providers():
    async def run():
        # Even if the raw LLM output hallucinated including the failed provider,
        # the advisor deterministically filters it out!
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "failure_classification": "PROVIDER_REJECTION",
                    "root_cause_explanation": "Provider declined.",
                    "suggested_recovery_path": "FIND_ALTERNATIVE_PROVIDER",
                    "is_recoverable": True,
                    "requires_human_intervention": False,
                    "alternative_provider_ids": ["hospital-b", "lab-c", "lab-d"],  # hospital-b is failed!
                    "suggested_counteroffer_quantity": None,
                    "remediation_steps": ["Query alternatives."],
                })
            ]
        )

        advisor = LLMRecoveryAdvisor(llm_client=LLMClient(provider=mock_provider))
        payload = {
            "error": "Provider rejected request",
            "failed_provider": "hospital-b",
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["hospital-b", "lab-c", "lab-d"],
        }
        advice = await advisor.advise_async(payload)

        # hospital-b must be deterministically stripped
        assert "hospital-b" not in advice.alternative_provider_ids
        assert "lab-c" in advice.alternative_provider_ids
        assert "lab-d" in advice.alternative_provider_ids

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 5: Suggests renegotiation for partial capacity
# -----------------------------------------------------------------------------
def test_suggests_renegotiation():
    async def run():
        advisor = LLMRecoveryAdvisor()
        payload = {
            "error": "Insufficient capacity: provider only has 2 slots available, counteroffer proposed",
            "failed_provider": "hospital-b",
            "partial_quantity": 2,
            "attempt": 1,
            "max_attempts": 3,
        }
        advice = await advisor.advise_async(payload)

        assert advice.failure_classification == FailureClassification.INSUFFICIENT_CAPACITY.value
        assert advice.suggested_recovery_path == RecoveryStrategy.RE_NEGOTIATE.value
        assert advice.is_recoverable is True
        assert advice.suggested_counteroffer_quantity == 2
        assert any("counteroffer" in step.lower() for step in advice.remediation_steps)

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 6: Identifies non-recoverable failures (security or privacy violations)
# -----------------------------------------------------------------------------
def test_identifies_non_recoverable_failures():
    async def run():
        advisor = LLMRecoveryAdvisor()
        payload = {
            "error": "Security policy violation: unauthorized access token and private_key exposure detected",
            "attempt": 1,
            "max_attempts": 3,
        }
        advice = await advisor.advise_async(payload)

        assert advice.is_recoverable is False
        assert advice.requires_human_intervention is True
        assert advice.suggested_recovery_path == RecoveryStrategy.ABORT_UNRECOVERABLE.value
        assert advice.failure_classification == FailureClassification.SECURITY_OR_POLICY_VIOLATION.value
        assert len(advice.alternative_provider_ids) == 0

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 7: Escalates unresolved disputes
# -----------------------------------------------------------------------------
def test_escalates_unresolved_disputes():
    async def run():
        advisor = LLMRecoveryAdvisor()
        payload = {
            "error": "Provider non-fulfillment: failed to deliver committed diagnostic slot post-confirmation",
            "failed_provider": "hospital-b",
            "is_dispute": True,
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["lab-c"],
        }
        advice = await advisor.advise_async(payload)

        assert advice.failure_classification == FailureClassification.PROVIDER_NON_FULFILLMENT.value
        assert advice.suggested_recovery_path == RecoveryStrategy.INITIATE_DISPUTE.value
        assert advice.requires_human_intervention is True
        assert "hospital-b" not in advice.alternative_provider_ids

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 8: Rejects invalid LLM recommendations and sanitizes safely
# -----------------------------------------------------------------------------
def test_rejects_invalid_llm_recommendations():
    async def run():
        # LLM returns an invalid/hallucinated strategy
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "failure_classification": "PROVIDER_REJECTION",
                    "root_cause_explanation": "Provider declined.",
                    "suggested_recovery_path": "HALLUCINATED_INVALID_MAGIC_STRATEGY",
                    "is_recoverable": True,
                    "requires_human_intervention": False,
                    "alternative_provider_ids": ["lab-c"],
                    "suggested_counteroffer_quantity": None,
                    "remediation_steps": ["Retry."],
                })
            ]
        )

        advisor = LLMRecoveryAdvisor(llm_client=LLMClient(provider=mock_provider))
        payload = {
            "error": "Provider Hospital-B rejected offer",
            "failed_provider": "hospital-b",
            "attempt": 1,
            "max_attempts": 3,
            "known_providers": ["hospital-b", "lab-c"],
        }
        # Sanitizer detects invalid strategy and falls back to deterministic diagnosis
        advice = await advisor.advise_async(payload)

        assert advice.suggested_recovery_path in {s.value for s in RecoveryStrategy}
        assert advice.suggested_recovery_path == RecoveryStrategy.FIND_ALTERNATIVE_PROVIDER.value
        assert "hospital-b" not in advice.alternative_provider_ids

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 9: Falls back when LLM is unavailable
# -----------------------------------------------------------------------------
def test_falls_back_when_llm_is_unavailable():
    async def run():
        mock_provider = MockLLMProvider(should_error=True, error_message="Simulated LLM Gateway Timeout")
        advisor = LLMRecoveryAdvisor(
            llm_client=LLMClient(provider=mock_provider),
            enable_fallback=True,
        )

        payload = {
            "error": "Temporary RPC connection timeout to Ethereum Coral node",
            "attempt": 1,
            "max_attempts": 3,
        }
        advice = await advisor.advise_async(payload)

        assert advice.failure_classification == FailureClassification.RPC_FAILURE.value
        assert advice.suggested_recovery_path == RecoveryStrategy.RETRY_WITH_BACKOFF.value
        assert advice.is_recoverable is True
        assert len(advice.remediation_steps) > 0

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 10: Never executes recovery actions automatically (advisory role only)
# -----------------------------------------------------------------------------
def test_never_executes_recovery_actions_automatically():
    """
    Ensure the advisor only outputs structured recommendations and does not
    contain or invoke any methods to release locks, submit transactions,
    or modify persistent workflow states.
    """
    advisor = LLMRecoveryAdvisor()
    assert not hasattr(advisor, "execute_transaction")
    assert not hasattr(advisor, "release_lock")
    assert not hasattr(advisor, "sign_transaction")
    assert not hasattr(advisor, "private_key")

    payload = {
        "error": "Expired lock detected on chain for lock ID 0x9876",
        "lock_id": "0x9876",
        "failed_provider": "hospital-b",
        "attempt": 1,
        "max_attempts": 3,
    }
    advice = advisor.advise(payload)

    # It recommends releasing the lock, but does not release it itself
    assert advice.suggested_recovery_path == RecoveryStrategy.RELEASE_LOCK_AND_RETRY.value
    assert isinstance(advice, RecoveryAdviceOutput)
    assert any("release" in step.lower() for step in advice.remediation_steps)


# -----------------------------------------------------------------------------
# Test 11: Escalates when max attempts are exceeded
# -----------------------------------------------------------------------------
def test_escalates_when_max_attempts_exceeded():
    async def run():
        advisor = LLMRecoveryAdvisor()
        payload = {
            "error": "Provider rejection on attempt 3",
            "attempt": 3,
            "max_attempts": 3,
            "failed_provider": "hospital-b",
        }
        advice = await advisor.advise_async(payload)

        assert advice.suggested_recovery_path == RecoveryStrategy.ESCALATE_TO_HUMAN.value
        assert advice.is_recoverable is False
        assert advice.requires_human_intervention is True
        assert "Exhausted maximum allowable recovery attempts" in advice.root_cause_explanation

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 12: RecoveryAgent integration with LLM advisor
# -----------------------------------------------------------------------------
def test_recovery_agent_with_llm_advisor_integration():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "failure_classification": "RPC_FAILURE",
                    "root_cause_explanation": "Network RPC disconnect to node endpoint.",
                    "suggested_recovery_path": "RETRY_WITH_BACKOFF",
                    "is_recoverable": True,
                    "requires_human_intervention": False,
                    "alternative_provider_ids": [],
                    "suggested_counteroffer_quantity": None,
                    "remediation_steps": ["Retry with exponential backoff."],
                })
            ]
        )
        advisor = LLMRecoveryAdvisor(llm_client=LLMClient(provider=mock_provider))
        agent = RecoveryAgent(advisor=advisor)

        wf_id = uuid4()
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="MonitoringAgent",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "RPC connection timed out",
                "failure_classification": "RPC_FAILURE",
                "attempt_number": 1,
            },
        )

        outcome = await agent.execute(msg)

        assert outcome.status == AgentStatus.COMPLETED
        assert outcome.data["selected_strategy"] == "RETRY_WITH_BACKOFF"
        assert "recovery_advice" in outcome.data
        assert outcome.data["recovery_advice"]["suggested_recovery_path"] == "RETRY_WITH_BACKOFF"
        assert outcome.data["recovery_advice"]["root_cause_explanation"] == "Network RPC disconnect to node endpoint."

    asyncio.run(run())
