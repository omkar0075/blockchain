import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.agents.recovery_agent import (
    FailureClassification,
    RecoveryAgent,
    RecoveryStrategy,
)
from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)


def make_request(
    city: str = "Metropolis",
    quantity: int = 2,
    excluded: Optional[list[str]] = None,
) -> ResourceRequest:
    now = datetime.now(timezone.utc)
    return ResourceRequest(
        request_id=uuid4(),
        requester_institution_id="hospital-a",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=quantity,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city=city),
        required_by=now + timedelta(hours=12),
        required_by_hours=12,
        constraints=ResourceConstraints(excluded_providers=excluded or []),
    )


def test_classifies_recoverable_and_non_recoverable_failures():
    """Verify that transient or capacity failures are classified recoverable, while security breaches are unrecoverable."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        req = make_request()

        # 1. Recoverable capacity failure
        msg_cap = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Insufficient capacity: only 1 slot available, 2 requested",
                "resource_request": req.model_dump(),
                "failed_provider_id": "hospital-b",
            },
        )
        res_cap = await agent.execute(msg_cap)
        assert res_cap.data["failure_classification"] == "INSUFFICIENT_CAPACITY"
        assert res_cap.data["is_recoverable"] is True
        assert res_cap.data["selected_strategy"] == "FIND_ALTERNATIVE_PROVIDER"

        # 2. Non-recoverable security violation
        msg_sec = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Security violation: private_key detected in payload",
                "resource_request": req.model_dump(),
            },
        )
        res_sec = await agent.execute(msg_sec)
        assert res_sec.data["failure_classification"] == "SECURITY_OR_POLICY_VIOLATION"
        assert res_sec.data["is_recoverable"] is False
        assert res_sec.data["selected_strategy"] == "ABORT_UNRECOVERABLE"

    asyncio.run(run())


def test_finds_alternative_providers():
    """Verify that when a provider fails or rejects, an alternative candidate is discovered excluding the failed provider."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        req = make_request()

        # Provider hospital-b failed
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Provider hospital-b rejected request due to temporary equipment downtime",
                "failure_classification": "PROVIDER_REJECTION",
                "failed_provider_id": "hospital-b",
                "resource_request": req.model_dump(),
            },
        )
        result = await agent.execute(msg)

        assert result.data["success"] is True
        assert result.data["is_recoverable"] is True
        assert result.data["selected_strategy"] == "FIND_ALTERNATIVE_PROVIDER"
        assert result.data["failed_provider_id"] == "hospital-b"
        # Alternative provider lab-c should be selected
        assert result.data["alternative_provider_id"] == "lab-c"
        assert len(result.data["alternative_offers"]) >= 1

    asyncio.run(run())


def test_handles_no_available_alternatives():
    """Verify that when no alternative providers exist in the region, the workflow escalates to human review."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        # Metropolis has hospital-b and lab-c. If both failed, none remain!
        req = make_request(excluded=["hospital-b", "lab-c"])

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Provider lab-c rejected",
                "failure_classification": "PROVIDER_REJECTION",
                "failed_provider_id": "lab-c",
                "resource_request": req.model_dump(),
            },
        )
        result = await agent.execute(msg)

        assert result.data["selected_strategy"] == "ESCALATE_TO_HUMAN"
        assert result.data["escalated"] is True
        assert "no alternative providers" in result.data["explanation"].lower()

    asyncio.run(run())


def test_re_enters_negotiation_safely():
    """Verify that candidate offers from the alternative provider are returned ready for multi-lateral negotiation."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        req = make_request()

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "hospital-b capacity locked by another requester",
                "failure_classification": "TRANSACTION_REVERT",
                "failed_provider_id": "hospital-b",
                "resource_request": req.model_dump(),
            },
        )
        result = await agent.execute(msg)

        offers = result.data.get("alternative_offers", [])
        assert len(offers) >= 1
        assert offers[0]["provider_id"] == "lab-c"

    asyncio.run(run())


def test_requires_fresh_verification():
    """Governance Invariant: Alternative providers must undergo fresh verification before execution."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        req = make_request()

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Provider rejection",
                "failed_provider_id": "hospital-b",
                "resource_request": req.model_dump(),
            },
        )
        result = await agent.execute(msg)

        # Invariant check:
        assert result.data["requires_fresh_verification"] is True

    asyncio.run(run())


def test_requires_authorization_before_retrying_blockchain_execution():
    """Governance Invariant: Recovered alternative offers cannot execute on-chain without human authorization."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        req = make_request()

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Capacity lock expired",
                "failed_provider_id": "hospital-b",
                "resource_request": req.model_dump(),
            },
        )
        result = await agent.execute(msg)

        # Invariant checks:
        assert result.data["requires_human_approval"] is True
        assert result.data["ready_for_blockchain"] is False

    asyncio.run(run())


def test_prevents_infinite_retries():
    """Safety Invariant: Prevent unbounded recovery loops by capping attempts and escalating."""
    async def run():
        # max recovery attempts = 3
        agent = RecoveryAgent(max_recovery_attempts=3)
        wf_id = uuid4()
        req = make_request()

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Provider rejection",
                "failed_provider_id": "hospital-b",
                "resource_request": req.model_dump(),
                "attempt_number": 4,  # Exceeded max 3
            },
        )
        result = await agent.execute(msg)

        assert result.data["selected_strategy"] == "ESCALATE_TO_HUMAN"
        assert result.data["escalated"] is True
        assert "exceeded maximum recovery attempts" in result.data["explanation"].lower()

    asyncio.run(run())


def test_preserves_original_workflow_history():
    """Audit Invariant: Preserve comprehensive lineage of all previous failures and root causes."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        req = make_request()

        prev_history = [
            {"attempt": 1, "classification": "RPC_FAILURE", "error": "RPC connection timed out"},
        ]

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Provider rejected quantity",
                "failure_classification": "PROVIDER_REJECTION",
                "failed_provider_id": "hospital-b",
                "resource_request": req.model_dump(),
                "failure_history": prev_history,
                "attempt_number": 2,
            },
        )
        result = await agent.execute(msg)

        history = result.data["failure_history"]
        assert len(history) == 2
        assert history[0]["classification"] == "RPC_FAILURE"
        assert history[1]["classification"] == "PROVIDER_REJECTION"
        assert history[1]["attempt"] == 2

    asyncio.run(run())


def test_escalates_unresolved_failures():
    """Verify that unresolved disputes or unexpected errors escalate to human institutional officers."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Unresolvable institutional state conflict",
                "failure_classification": "UNKNOWN",
            },
        )
        result = await agent.execute(msg)

        assert result.data["selected_strategy"] == "ESCALATE_TO_HUMAN"
        assert result.data["escalated"] is True

    asyncio.run(run())


def test_does_not_silently_cancel_commitments():
    """Invariant: An active on-chain commitment cannot be silently dropped; must trigger arbitration dispute."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Provider failed to deliver medical slot before SLA deadline",
                "failure_classification": "PROVIDER_NON_FULFILLMENT",
                "commitment_status": "COMMITTED",
                "reservation_id": "0xcommitment123",
                "failed_provider_id": "lab-c",
            },
        )
        result = await agent.execute(msg)

        assert result.data["selected_strategy"] == "INITIATE_DISPUTE"
        assert result.data["reservation_id"] == "0xcommitment123"
        assert result.data["escalated"] is True
        assert "cannot silently cancel" in result.data["explanation"].lower()

    asyncio.run(run())


def test_handles_expired_capacity_locks():
    """Verify handling of expired locks by recording the release and discovering an alternative."""
    async def run():
        agent = RecoveryAgent()
        wf_id = uuid4()
        req = make_request()

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="RecoveryAgent",
            message_type="RECOVER_FAILURE",
            payload={
                "error": "Capacity lock expired before institutional signature was submitted",
                "failure_classification": "EXPIRED_LOCK",
                "lock_id": "0xexpired_lock_44",
                "failed_provider_id": "hospital-b",
                "resource_request": req.model_dump(),
            },
        )
        result = await agent.execute(msg)

        assert result.data["released_lock_id"] == "0xexpired_lock_44"
        assert result.data["selected_strategy"] == "FIND_ALTERNATIVE_PROVIDER"
        assert result.data["alternative_provider_id"] == "lab-c"

    asyncio.run(run())
