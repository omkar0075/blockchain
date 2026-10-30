import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest

from app.agents.discovery_agent import DiscoveryAgent
from app.agents.negotiation_agent import NegotiationAgent
from app.agents.orchestrator import (
    WorkflowOrchestrator,
    WorkflowRecord,
    WorkflowState,
    create_default_registry,
)
from app.agents.provider_agent import ProviderAgent
from app.agents.registry import AgentRegistry
from app.agents.request_agent import RequestAgent
from app.agents.verification_agent import VerificationAgent
from app.schemas.agent_messages import AgentMessage, AgentResult, AgentStatus
from app.schemas.provider_offer import ProviderCost
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)
from app.schemas.verification import VerificationStatus


def make_valid_request() -> ResourceRequest:
    now = datetime.now(timezone.utc)
    return ResourceRequest(
        request_id=uuid4(),
        requester_institution_id="hospital-a",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=2,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city="Metropolis"),
        required_by=now + timedelta(hours=12),
        required_by_hours=12,
    )


def test_completes_successful_workflow_through_verification():
    """Verify an end-to-end pipeline run from natural language request to verified allocation."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        nl_input = "Urgent requirement for 2 diagnostic slots in Metropolis within 12 hours"

        record = await orchestrator.run(nl_input)

        # 1. State must pause at WAITING_FOR_APPROVAL
        assert record.state == WorkflowState.WAITING_FOR_APPROVAL

        # 2. Request extracted and validated
        assert record.request is not None
        assert record.request.quantity == 2
        assert record.request.resource_type == ResourceType.DIAGNOSTIC_SLOT

        # 3. Discovered candidates & collected offers
        assert len(record.discovered_candidates) >= 1
        assert len(record.provider_offers) >= 1

        # 4. Negotiation proposal selected
        assert record.selected_proposal is not None
        assert record.selected_proposal.proposed_quantity == 2

        # 5. Verification completed and passed
        assert record.verification_result is not None
        assert record.verification_result.status == VerificationStatus.VERIFIED
        assert record.verification_result.checks.is_all_passed() is True

    asyncio.run(run())


def test_stops_when_request_needs_clarification():
    """Verify that ambiguous or missing information stops the workflow at NEEDS_CLARIFICATION."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        # Missing quantity and location
        ambiguous_input = "We urgently need diagnostic slots please send details"

        record = await orchestrator.run(ambiguous_input)

        assert record.state == WorkflowState.NEEDS_CLARIFICATION
        assert record.clarification_prompt is not None
        assert any(term in record.clarification_prompt.lower() for term in ("how many", "quantity", "units", "slots"))
        # Pipeline must not have progressed to discovery
        assert len(record.discovered_candidates) == 0
        assert len(record.provider_offers) == 0

    asyncio.run(run())


def test_stops_when_no_provider_is_found():
    """Verify that a request with no matching provider stops at NO_PROVIDERS_FOUND."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        now = datetime.now(timezone.utc)
        # Requesting blood units in a city with no blood bank
        req = ResourceRequest(
            requester_institution_id="hospital-a",
            resource_type=ResourceType.BLOOD_UNIT,
            quantity=10,
            urgency=UrgencyLevel.URGENT,
            location=LocationArea(city="Atlantis"),  # Non-existent service area
            required_by=now + timedelta(hours=12),
            required_by_hours=12,
        )

        record = await orchestrator.run(req)

        assert record.state == WorkflowState.NO_PROVIDERS_FOUND
        assert len(record.discovered_candidates) == 0
        assert record.selected_proposal is None

    asyncio.run(run())


def test_handles_provider_agent_failure():
    """Verify that if candidate provider agents fail or return no offers, workflow halts cleanly."""
    async def run():
        registry = AgentRegistry()
        registry.register(RequestAgent())
        # Discovery will return lab-c
        registry.register(DiscoveryAgent())
        registry.register(NegotiationAgent())
        registry.register(VerificationAgent())

        # Provider lab-c has NO capacity for diagnostic slots (0 available)
        failing_provider = ProviderAgent(
            provider_id="lab-c",
            provider_name="Central Diagnostic Lab C",
            institution_type="diagnostic_lab",
            location=LocationArea(city="Metropolis"),
            inventory={"diagnostic_slot": {"total": 12, "available": 0}},
            delivery_hours=2,
        )
        registry.register(failing_provider)

        orchestrator = WorkflowOrchestrator(registry=registry)
        req = make_valid_request()

        record = await orchestrator.run(req)

        assert record.state == WorkflowState.NO_OFFERS_RECEIVED
        assert len(record.provider_offers) == 0
        assert record.selected_proposal is None

    asyncio.run(run())


def test_handles_negotiation_failure():
    """Verify that if negotiation policy rejects all offers (e.g. over-budget), workflow halts at NEGOTIATION_FAILED."""
    async def run():
        registry = AgentRegistry()
        registry.register(RequestAgent())
        registry.register(DiscoveryAgent())
        # NegotiationAgent with strict max budget of $50
        registry.register(NegotiationAgent(default_max_budget=50.0))
        registry.register(VerificationAgent())

        # lab-c charges $200 (exceeds budget)
        expensive_provider = ProviderAgent(
            provider_id="lab-c",
            provider_name="Central Diagnostic Lab C",
            institution_type="diagnostic_lab",
            location=LocationArea(city="Metropolis"),
            inventory={"diagnostic_slot": {"total": 12, "available": 8}},
            pricing={"diagnostic_slot": ProviderCost(amount=200.0, currency="USD")},
            delivery_hours=2,
        )
        registry.register(expensive_provider)

        orchestrator = WorkflowOrchestrator(registry=registry)
        req = make_valid_request()

        record = await orchestrator.run(req)

        assert record.state == WorkflowState.NEGOTIATION_FAILED
        assert record.selected_proposal is None

    asyncio.run(run())


def test_stops_on_failed_verification():
    """Verify that failed verification halts workflow at VERIFICATION_FAILED without proceeding to approval."""
    async def run():
        registry = AgentRegistry()
        registry.register(RequestAgent())
        registry.register(DiscoveryAgent())
        registry.register(NegotiationAgent())

        # VerificationAgent configured with inactive provider for lab-c
        strict_reg = {
            "lab-c": {
                "id": "lab-c",
                "name": "Central Diagnostic Lab C",
                "wallet_address": "0xb05049cc2A5a1c28979DEafc82B82749681DA92C",
                "is_active": False,  # Suspended!
                "authorized_resource_types": ["diagnostic_slot"],
                "service_areas": ["Metropolis"],
            }
        }
        registry.register(VerificationAgent(institutional_registry=strict_reg))

        provider = ProviderAgent(
            provider_id="lab-c",
            provider_name="Central Diagnostic Lab C",
            institution_type="diagnostic_lab",
            location=LocationArea(city="Metropolis"),
            inventory={"diagnostic_slot": {"total": 12, "available": 8}},
            delivery_hours=2,
        )
        registry.register(provider)

        orchestrator = WorkflowOrchestrator(registry=registry)
        req = make_valid_request()

        record = await orchestrator.run(req)

        assert record.state == WorkflowState.VERIFICATION_FAILED
        assert record.verification_result is not None
        assert record.verification_result.status == VerificationStatus.REJECTED
        assert record.approved_by is None

    asyncio.run(run())


def test_waits_for_human_approval():
    """Verify that workflow halts at WAITING_FOR_APPROVAL and requires explicit institutional approval."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        req = make_valid_request()

        record = await orchestrator.run(req)

        # Reached pause state
        assert record.state == WorkflowState.WAITING_FOR_APPROVAL
        assert record.approved_by is None

        # Record attributable approval
        updated = orchestrator.record_approval(record.workflow_id, approved_by="Dr. Alice Smith (Chief Medical Officer)")
        assert updated.state == WorkflowState.APPROVED
        assert updated.approved_by == "Dr. Alice Smith (Chief Medical Officer)"
        assert updated.approved_at is not None
        assert updated.context is not None
        assert updated.context.approval_required is False

    asyncio.run(run())


def test_prevents_blockchain_execution_before_approval():
    """Governance Invariant: Blockchain execution before institutional approval is strictly prohibited."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        req = make_valid_request()

        record = await orchestrator.run(req)
        assert record.state == WorkflowState.WAITING_FOR_APPROVAL

        # Attempting execution before approval must raise PermissionError
        with pytest.raises(PermissionError) as exc_info:
            orchestrator.execute_blockchain_commitment(record.workflow_id)

        assert "institutional approval is required" in str(exc_info.value).lower()

    asyncio.run(run())


def test_preserves_workflow_id_across_agent_calls():
    """Lineage Invariant: Every agent call and resulting record must preserve the originating workflow_id."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        expected_wf_id = uuid4()
        req = make_valid_request()

        record = await orchestrator.run(req, workflow_id=expected_wf_id)

        assert record.workflow_id == expected_wf_id
        assert record.context is not None
        assert record.context.workflow_id == expected_wf_id
        assert record.selected_proposal is not None
        assert record.selected_proposal.workflow_id == expected_wf_id
        assert record.verification_result is not None
        assert record.verification_result.workflow_id == expected_wf_id

    asyncio.run(run())


def test_records_workflow_state_transitions():
    """Audit Invariant: Every state transition is recorded with timestamp and agent attribution."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        req = make_valid_request()

        record = await orchestrator.run(req)

        state_sequence = [t.to_state for t in record.transitions]
        expected_sequence = [
            WorkflowState.PARSING_REQUEST,
            WorkflowState.DISCOVERING_PROVIDERS,
            WorkflowState.REQUESTING_OFFERS,
            WorkflowState.NEGOTIATING,
            WorkflowState.VERIFYING,
            WorkflowState.WAITING_FOR_APPROVAL,
        ]

        for expected in expected_sequence:
            assert expected in state_sequence, f"Missing expected state transition: {expected}"

        for transition in record.transitions:
            assert transition.timestamp.tzinfo is not None
            assert transition.agent is not None

    asyncio.run(run())


def test_prevents_duplicate_execution():
    """Safety Invariant: Prevent duplicate execution of an existing workflow ID."""
    async def run():
        orchestrator = WorkflowOrchestrator()
        wf_id = uuid4()
        req = make_valid_request()

        await orchestrator.run(req, workflow_id=wf_id)

        # Attempting to re-run with the exact same workflow_id
        with pytest.raises(ValueError) as exc_info:
            await orchestrator.run(req, workflow_id=wf_id)

        assert "already been executed" in str(exc_info.value).lower()

    asyncio.run(run())
