import asyncio
import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4
import pytest

from app.agents.discovery_agent import (
    DEFAULT_PROVIDER_CATALOG,
    DiscoveryAgent,
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
    resource_type: ResourceType = ResourceType.DIAGNOSTIC_SLOT,
    quantity: int = 2,
    city: str = "Metropolis",
    urgency: UrgencyLevel = UrgencyLevel.ROUTINE,
    requester_id: str = "hospital-a",
    preferred: Optional[list[str]] = None,
    excluded: Optional[list[str]] = None,
) -> ResourceRequest:
    now = datetime.now(timezone.utc)
    return ResourceRequest(
        requester_institution_id=requester_id,
        resource_type=resource_type,
        quantity=quantity,
        urgency=urgency,
        location=LocationArea(city=city),
        required_by=now + timedelta(hours=24),
        required_by_hours=24,
        constraints=ResourceConstraints(
            preferred_providers=preferred or [],
            excluded_providers=excluded or [],
        ),
    )


def test_finds_matching_provider():
    """Verify DiscoveryAgent identifies eligible candidate providers."""
    async def run():
        agent = DiscoveryAgent()
        req = make_request(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=2, city="Metropolis")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        result = await agent.execute(msg)

        assert isinstance(result, AgentResult)
        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is True
        assert result.data["matches_found"] > 0

        offers = result.data["candidate_offers"]
        provider_ids = [o["provider_id"] for o in offers]
        assert "lab-c" in provider_ids or "hospital-b" in provider_ids

    asyncio.run(run())


def test_filters_out_wrong_resource_type():
    """Verify providers without the requested resource type are excluded."""
    async def run():
        agent = DiscoveryAgent()
        # Request blood_unit (lab-c only has diagnostic_slot, blood-bank-d is in Gotham)
        req = make_request(resource_type=ResourceType.BLOOD_UNIT, quantity=5, city="Gotham")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        result = await agent.execute(msg)
        assert result.data["matches_found"] == 1
        assert result.data["candidate_offers"][0]["provider_id"] == "blood-bank-d"

    asyncio.run(run())


def test_filters_providers_outside_service_area():
    """Verify providers outside the requested city/location are filtered out."""
    async def run():
        agent = DiscoveryAgent()
        # Request blood units in Metropolis (blood-bank-d is located in Gotham)
        req = make_request(resource_type=ResourceType.BLOOD_UNIT, quantity=5, city="Metropolis")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        result = await agent.execute(msg)
        assert result.data["matches_found"] == 0
        assert "No eligible providers found" in result.data["message"]

    asyncio.run(run())


def test_excludes_inactive_providers():
    """Verify inactive providers (e.g. ambulance-service-e) are never discovered."""
    async def run():
        agent = DiscoveryAgent()
        req = make_request(resource_type=ResourceType.AMBULANCE, quantity=1, city="Metropolis")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        result = await agent.execute(msg)
        assert result.data["matches_found"] == 0

    asyncio.run(run())


def test_excludes_insufficient_capacity():
    """Verify providers with available capacity less than requested are excluded."""
    async def run():
        agent = DiscoveryAgent()
        # hospital-b has 4 diagnostic slots, lab-c has 8 available
        # If requesting 10 diagnostic slots, both should be excluded!
        req = make_request(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=10, city="Metropolis")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        result = await agent.execute(msg)
        assert result.data["matches_found"] == 0

    asyncio.run(run())


def test_ranks_multiple_matching_providers():
    """Verify ranking orders preferred providers first, followed by delivery SLA."""
    async def run():
        agent = DiscoveryAgent()
        # Request 2 diagnostic slots in Metropolis (both lab-c and hospital-b qualify)
        # Explicitly make hospital-b preferred even though lab-c has shorter delivery SLA
        req = make_request(
            resource_type=ResourceType.DIAGNOSTIC_SLOT,
            quantity=2,
            city="Metropolis",
            preferred=["hospital-b"],
        )

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        result = await agent.execute(msg)
        assert result.data["matches_found"] == 2
        offers = result.data["candidate_offers"]

        # hospital-b must be ranked first because it is in preferred_providers
        assert offers[0]["provider_id"] == "hospital-b"
        assert offers[1]["provider_id"] == "lab-c"

    asyncio.run(run())


def test_handles_no_matching_providers():
    """Verify clean response when no providers exist for a niche resource."""
    async def run():
        agent = DiscoveryAgent()
        req = make_request(resource_type=ResourceType.OXYGEN_CYLINDER, quantity=2, city="Metropolis")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        result = await agent.execute(msg)
        assert result.data["matches_found"] == 0
        assert len(result.data["candidate_offers"]) == 0
        assert "No eligible providers found" in result.data["message"]

    asyncio.run(run())


def test_handles_invalid_request():
    """Verify malformed input raises a clear ValueError resulting in FAILED status."""
    async def run():
        agent = DiscoveryAgent()
        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": {"not": "valid"}},
        )

        result = await agent.execute(msg)
        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "DiscoveryAgent requires a valid ResourceRequest" in result.error.message

    asyncio.run(run())


def test_does_not_modify_provider_capacity():
    """Invariant test: discovery must never alter or lock capacity in the provider catalog."""
    async def run():
        catalog_copy = copy.deepcopy(DEFAULT_PROVIDER_CATALOG)
        agent = DiscoveryAgent(catalog=catalog_copy)
        req = make_request(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=3, city="Metropolis")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="DiscoveryAgent",
            message_type="DISCOVER_RESOURCES",
            payload={"resource_request": req.model_dump()},
        )

        await agent.execute(msg)

        # Invariant check: capacities remain identical before and after discovery
        assert catalog_copy == DEFAULT_PROVIDER_CATALOG

    asyncio.run(run())
