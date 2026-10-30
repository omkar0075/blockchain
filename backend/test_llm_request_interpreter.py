import asyncio
import json
from uuid import uuid4
import pytest

from app.agents.request_agent import RequestAgent
from app.llm.client import LLMClient
from app.llm.provider import MockLLMProvider
from app.llm.request_interpreter import LLMRequestParser
from app.schemas.agent_messages import AgentMessage
from app.schemas.resource_request import ResourceType, UrgencyLevel


# -----------------------------------------------------------------------------
# Test 1: Interprets a clear diagnostic-slot request
# -----------------------------------------------------------------------------
def test_interprets_clear_diagnostic_slot_request():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "resource_type": "diagnostic_slot",
                    "quantity": 3,
                    "urgency": "urgent",
                    "location": "Pune",
                    "required_by_hours": 18,
                    "is_ambiguous": False,
                    "missing_fields": [],
                    "clarification_prompt": None,
                })
            ]
        )
        parser = LLMRequestParser(llm_client=LLMClient(provider=mock_provider))
        agent = RequestAgent(parser=parser)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="User",
            receiver="RequestAgent",
            message_type="PARSE_REQUEST",
            payload={
                "text": "Hospital A needs 3 diagnostic slots near Pune tomorrow morning, high urgency.",
                "requester_institution_id": "hospital-a",
            },
        )

        res = await agent.execute(msg)
        assert res.data["success"] is True
        assert res.data["clarification_needed"] is False

        req = res.data["resource_request"]
        assert req["resource_type"] == ResourceType.DIAGNOSTIC_SLOT.value
        assert req["quantity"] == 3
        assert req["location"]["city"] == "Pune"
        assert req["urgency"] == UrgencyLevel.URGENT.value

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 2: Extracts written quantities ("three", "ten")
# -----------------------------------------------------------------------------
def test_extracts_written_quantities():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "resource_type": "icu_bed",
                    "quantity": 3,
                    "urgency": "urgent",
                    "location": "Metropolis",
                    "required_by_hours": 12,
                    "is_ambiguous": False,
                    "missing_fields": [],
                })
            ]
        )
        parser = LLMRequestParser(llm_client=LLMClient(provider=mock_provider))

        result = await parser.parse_async(
            text="City Clinic requires three ICU beds in Metropolis within 12 hours",
            requester_institution_id="clinic-1",
        )

        assert result["quantity"] == 3
        assert result["resource_type"] == ResourceType.ICU_BED
        assert result["location"].city == "Metropolis"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 3: Identifies missing quantity
# -----------------------------------------------------------------------------
def test_identifies_missing_quantity():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "resource_type": "diagnostic_slot",
                    "quantity": None,
                    "urgency": "routine",
                    "location": "Pune",
                    "required_by_hours": 24,
                    "is_ambiguous": True,
                    "missing_fields": ["quantity"],
                    "clarification_prompt": "How many diagnostic slots are required?",
                })
            ]
        )
        parser = LLMRequestParser(llm_client=LLMClient(provider=mock_provider))
        agent = RequestAgent(parser=parser)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="User",
            receiver="RequestAgent",
            message_type="PARSE_REQUEST",
            payload={"text": "Hospital B needs diagnostic slots in Pune"},
        )

        res = await agent.execute(msg)
        assert res.data["clarification_needed"] is True
        assert "quantity" in res.data["missing_fields"]
        assert "How many" in res.data["clarification_prompt"]

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 4: Identifies missing location
# -----------------------------------------------------------------------------
def test_identifies_missing_location():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "resource_type": "blood_unit",
                    "quantity": 5,
                    "urgency": "urgent",
                    "location": None,
                    "required_by_hours": 6,
                    "is_ambiguous": True,
                    "missing_fields": ["location"],
                    "clarification_prompt": "What is the facility location or service area?",
                })
            ]
        )
        parser = LLMRequestParser(llm_client=LLMClient(provider=mock_provider))
        agent = RequestAgent(parser=parser)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="User",
            receiver="RequestAgent",
            message_type="PARSE_REQUEST",
            payload={"text": "Urgent request for 5 blood units needed in 6 hours"},
        )

        res = await agent.execute(msg)
        assert res.data["clarification_needed"] is True
        assert "location" in res.data["missing_fields"]

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 5: Handles ambiguous resource types
# -----------------------------------------------------------------------------
def test_handles_ambiguous_resource_types():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "resource_type": None,
                    "quantity": 5,
                    "urgency": "critical",
                    "location": "Metropolis",
                    "required_by_hours": 2,
                    "is_ambiguous": True,
                    "missing_fields": ["resource_type"],
                    "clarification_prompt": "Multiple resource types requested. Please coordinate one resource type per request.",
                })
            ]
        )
        parser = LLMRequestParser(llm_client=LLMClient(provider=mock_provider))
        agent = RequestAgent(parser=parser)

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="User",
            receiver="RequestAgent",
            message_type="PARSE_REQUEST",
            payload={"text": "We need both 2 ICU beds and 3 ambulances urgently in Metropolis"},
        )

        res = await agent.execute(msg)
        assert res.data["clarification_needed"] is True
        assert "resource_type" in res.data["missing_fields"]

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 6: Rejects unsupported resource types
# -----------------------------------------------------------------------------
def test_rejects_unsupported_resource_types():
    async def run():
        parser = LLMRequestParser()
        with pytest.raises(ValueError, match="Unsupported resource type 'surgical_robot'"):
            await parser.parse_async(
                text="Hospital A urgently needs da Vinci surgical robot in Pune",
                requester_institution_id="hospital-a",
            )

        with pytest.raises(ValueError, match="Unsupported resource type 'helicopter'"):
            await parser.parse_async(
                text="Need emergency helicopter air ambulance chopper in Metropolis",
                requester_institution_id="hospital-a",
            )

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 7: Handles invalid LLM output (malformed JSON handled via fallback)
# -----------------------------------------------------------------------------
def test_handles_invalid_llm_output():
    async def run():
        # LLM returns invalid malformed text instead of JSON
        mock_provider = MockLLMProvider(
            canned_responses=["Invalid Non-JSON response from LLM"]
        )
        # Fallback parser handles the plain text request deterministically
        parser = LLMRequestParser(
            llm_client=LLMClient(provider=mock_provider, max_retries=0),
            enable_fallback=True,
        )

        result = await parser.parse_async(
            text="Need 4 diagnostic slots in Metropolis within 12 hours",
            requester_institution_id="hospital-a",
        )

        assert result["quantity"] == 4
        assert result["resource_type"] == ResourceType.DIAGNOSTIC_SLOT
        assert result["location"].city == "Metropolis"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 8: Falls back when the LLM API is unavailable
# -----------------------------------------------------------------------------
def test_falls_back_when_llm_api_unavailable():
    async def run():
        # LLM provider crashes or encounters rate limit
        mock_provider = MockLLMProvider(
            should_error=True,
            error_message="Upstream model outage (HTTP 503)",
        )
        parser = LLMRequestParser(
            llm_client=LLMClient(provider=mock_provider, max_retries=0),
            enable_fallback=True,
        )

        result = await parser.parse_async(
            text="Hospital A urgently requires 2 ICU beds in Metropolis within 6 hours",
            requester_institution_id="hospital-a",
        )

        # Fallback DeterministicRequestParser successfully resolved the request
        assert result["quantity"] == 2
        assert result["resource_type"] == ResourceType.ICU_BED
        assert result["urgency"] == UrgencyLevel.URGENT
        assert result["location"].city == "Metropolis"

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 9: Preserves urgency and deadline correctly
# -----------------------------------------------------------------------------
def test_preserves_urgency_and_deadline_correctly():
    async def run():
        mock_provider = MockLLMProvider(
            canned_responses=[
                json.dumps({
                    "resource_type": "hospital_bed",
                    "quantity": 10,
                    "urgency": "critical",
                    "location": "Metropolis",
                    "required_by_hours": 4,
                    "is_ambiguous": False,
                    "missing_fields": [],
                })
            ]
        )
        parser = LLMRequestParser(llm_client=LLMClient(provider=mock_provider))

        result = await parser.parse_async(
            text="Need 10 hospital beds in Metropolis, critical emergency within 4 hours",
            requester_institution_id="hospital-a",
        )

        assert result["urgency"] == UrgencyLevel.CRITICAL
        assert result["required_by_hours"] == 4
        assert result["quantity"] == 10

    asyncio.run(run())


# -----------------------------------------------------------------------------
# Test 10: Rejects patient-sensitive information
# -----------------------------------------------------------------------------
def test_rejects_patient_sensitive_information():
    async def run():
        parser = LLMRequestParser()

        # Reject SSN
        with pytest.raises(ValueError, match="Privacy violation"):
            await parser.parse_async(
                text="Patient John Doe with SSN 123-45-6789 needs 1 ICU bed in Pune",
                requester_institution_id="hospital-a",
            )

        # Reject MRN keyword
        with pytest.raises(ValueError, match="Privacy violation"):
            await parser.parse_async(
                text="Transfer request for MRN-887766 needing diagnostic slot in Metropolis",
                requester_institution_id="hospital-a",
            )

    asyncio.run(run())
