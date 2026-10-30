import asyncio
from uuid import uuid4
import pytest

from app.agents.request_agent import (
    DeterministicRequestParser,
    RequestAgent,
)
from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from app.schemas.resource_request import ResourceType, UrgencyLevel


def make_request_message(text: str, requester_id: str = "hospital-a") -> AgentMessage:
    return AgentMessage(
        workflow_id=uuid4(),
        sender="UserOrchestrator",
        receiver="RequestAgent",
        message_type="PARSE_REQUEST",
        payload={
            "text": text,
            "requester_institution_id": requester_id,
        },
    )


def test_valid_natural_language_request():
    """Verify clean extraction of resource, quantity, urgency, location, and deadline."""
    async def run():
        agent = RequestAgent()
        msg = make_request_message(
            "Emergency: We critically need 3 ICU beds in Metropolis within 6 hours"
        )

        result = await agent.execute(msg)

        assert isinstance(result, AgentResult)
        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is True
        assert result.data["clarification_needed"] is False

        req = result.data["resource_request"]
        assert req["resource_type"] == ResourceType.ICU_BED.value
        assert req["quantity"] == 3
        assert req["urgency"] == UrgencyLevel.CRITICAL.value
        assert req["location"]["city"] == "Metropolis"
        assert req["required_by_hours"] == 6

    asyncio.run(run())


def test_missing_quantity_triggers_clarification():
    """Verify request with missing quantity transitions to WAITING_FOR_APPROVAL."""
    async def run():
        agent = RequestAgent()
        msg = make_request_message("Need hospital beds in Metropolis ASAP")

        result = await agent.execute(msg)

        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["success"] is False
        assert result.data["clarification_needed"] is True
        assert "quantity" in result.data["missing_fields"]
        assert "How many units or slots are required?" in result.data["clarification_prompt"]

    asyncio.run(run())


def test_missing_location_triggers_clarification():
    """Verify request with missing location transitions to WAITING_FOR_APPROVAL."""
    async def run():
        agent = RequestAgent()
        msg = make_request_message("Urgent request for 5 blood units within 12 hours")

        result = await agent.execute(msg)

        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["success"] is False
        assert result.data["clarification_needed"] is True
        assert "location" in result.data["missing_fields"]
        assert "What is the service area, city, or postal code" in result.data["clarification_prompt"]

    asyncio.run(run())


def test_ambiguous_resource_type():
    """Verify multiple conflicting resource types trigger clarification."""
    async def run():
        agent = RequestAgent()
        msg = make_request_message("Need 2 ventilators and 10 blood units in Gotham")

        result = await agent.execute(msg)

        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["clarification_needed"] is True
        assert result.data["reason"] == "ambiguous_resource_type"
        assert "Please specify one resource type per coordination workflow" in result.data["clarification_prompt"]

    asyncio.run(run())


def test_unsupported_resource_type():
    """Verify queries with no recognized resource type ask for clarification."""
    async def run():
        agent = RequestAgent()
        msg = make_request_message("Need 4 cybernetic hyper-drills in Springfield")

        result = await agent.execute(msg)

        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["clarification_needed"] is True
        assert "resource_type" in result.data["missing_fields"]
        assert "What specific healthcare resource is required" in result.data["clarification_prompt"]

    asyncio.run(run())


def test_pii_strictly_rejected():
    """Verify queries containing patient identifying information fail with privacy violation."""
    async def run():
        agent = RequestAgent()
        forbidden_queries = [
            "Need 2 ICU beds in Metropolis for patient name: John Doe",
            "Urgent 1 blood unit in Metropolis, SSN: 123-45-6789",
            "Need 3 hospital beds in Gotham, MRN: MRN-998811",
            "Need an ambulance in Metropolis for patient_id: PT-4422",
        ]

        for query in forbidden_queries:
            msg = make_request_message(query)
            result = await agent.execute(msg)
            assert result.status == AgentStatus.FAILED
            assert result.error is not None
            assert result.error.error_code == "ValueError"
            assert "Privacy violation" in result.error.message

    asyncio.run(run())


def test_written_numbers_and_postal_code_location():
    """Verify parsing written English numbers (e.g. 'five') and postal code routing."""
    async def run():
        agent = RequestAgent()
        msg = make_request_message("Need five diagnostic slots in postal code 560001 within 48 hours")

        result = await agent.execute(msg)

        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is True
        req = result.data["resource_request"]
        assert req["quantity"] == 5
        assert req["resource_type"] == ResourceType.DIAGNOSTIC_SLOT.value
        assert req["location"]["postal_code"] == "560001"
        assert req["required_by_hours"] == 48

    asyncio.run(run())


def test_invalid_payload_rejected():
    """Verify execute() fails gracefully when required text is missing from payload."""
    async def run():
        agent = RequestAgent()
        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Sender",
            receiver="RequestAgent",
            message_type="PARSE_REQUEST",
            payload={},  # Empty payload
        )

        result = await agent.execute(msg)
        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "RequestAgent requires 'text', 'query', or 'raw_request'" in result.error.message

    asyncio.run(run())
