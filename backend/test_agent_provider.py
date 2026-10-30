import asyncio
import copy
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
import pytest

from app.agents.provider_agent import ProviderAgent
from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from app.schemas.provider_offer import OfferStatus, ProviderCost, ProviderOffer
from app.schemas.resource_request import (
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)


def make_test_provider(
    provider_id: str = "lab-c",
    provider_name: str = "Central Diagnostic Lab C",
    city: str = "Metropolis",
    is_active: bool = True,
    inventory: Optional[dict] = None,
    delivery_hours: int = 2,
) -> ProviderAgent:
    default_inv = {
        "diagnostic_slot": {"total": 12, "available": 8},
        "icu_bed": {"total": 5, "available": 2},
    }
    pricing = {
        "diagnostic_slot": ProviderCost(amount=150.0, currency="USD"),
    }
    return ProviderAgent(
        provider_id=provider_id,
        provider_name=provider_name,
        institution_type="diagnostic_lab",
        location=LocationArea(city=city, postal_code="560001", service_area="Metropolis"),
        is_active=is_active,
        inventory=inventory or default_inv,
        pricing=pricing,
        delivery_hours=delivery_hours,
        offer_validity_hours=6,
    )


def make_request_message(
    resource_type: ResourceType = ResourceType.DIAGNOSTIC_SLOT,
    quantity: int = 2,
    city: str = "Metropolis",
    requester_id: str = "hospital-a",
) -> AgentMessage:
    now = datetime.now(timezone.utc)
    req = ResourceRequest(
        requester_institution_id=requester_id,
        resource_type=resource_type,
        quantity=quantity,
        urgency=UrgencyLevel.ROUTINE,
        location=LocationArea(city=city, postal_code="560001"),
        required_by=now + timedelta(hours=24),
    )
    return AgentMessage(
        workflow_id=uuid4(),
        sender="Orchestrator",
        receiver="ProviderAgent_lab-c",
        message_type="REQUEST_OFFER",
        payload={"resource_request": req.model_dump()},
    )


def test_generates_valid_offer():
    """Verify ProviderAgent produces a fully compliant ProviderOffer."""
    async def run():
        provider = make_test_provider()
        msg = make_request_message(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=2)

        result = await provider.execute(msg)

        assert isinstance(result, AgentResult)
        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is True
        assert result.data["eligible"] is True

        offer_dict = result.data["offer"]
        offer = ProviderOffer.model_validate(offer_dict)
        assert offer.provider_id == "lab-c"
        assert offer.offered_quantity == 2
        assert offer.resource_type == ResourceType.DIAGNOSTIC_SLOT
        assert offer.status == OfferStatus.OFFERED
        assert offer.onchain_verified is False  # Invariant: offer is not a reservation
        assert offer.cost.amount == 150.0

    asyncio.run(run())


def test_rejects_unsupported_resource_types():
    """Verify ProviderAgent rejects requests for unlisted resources."""
    async def run():
        provider = make_test_provider()
        # Request blood_unit (lab-c only has diagnostic_slot and icu_bed)
        msg = make_request_message(resource_type=ResourceType.BLOOD_UNIT, quantity=1)

        result = await provider.execute(msg)
        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is False
        assert result.data["eligible"] is False
        assert "not supported" in result.data["reason"]

    asyncio.run(run())


def test_rejects_insufficient_capacity():
    """Verify ProviderAgent rejects requests when requested quantity exceeds available uncommitted slots."""
    async def run():
        provider = make_test_provider()  # 8 diagnostic slots available
        # Request 10 slots
        msg = make_request_message(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=10)

        result = await provider.execute(msg)
        assert result.data["success"] is False
        assert result.data["eligible"] is False
        assert "Insufficient available capacity" in result.data["reason"]

    asyncio.run(run())


def test_rejects_inactive_providers():
    """Verify inactive providers do not issue offers."""
    async def run():
        provider = make_test_provider(is_active=False)
        msg = make_request_message(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=1)

        result = await provider.execute(msg)
        assert result.data["success"] is False
        assert result.data["eligible"] is False
        assert "currently inactive" in result.data["reason"]

    asyncio.run(run())


def test_rejects_requests_outside_service_area():
    """Verify requests outside the provider's location/service area are rejected."""
    async def run():
        provider = make_test_provider(city="Metropolis")
        # Request in Gotham
        msg = make_request_message(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=1, city="Gotham")

        result = await provider.execute(msg)
        assert result.data["success"] is False
        assert result.data["eligible"] is False
        assert "outside this provider's operational service area" in result.data["reason"]

    asyncio.run(run())


def test_sets_valid_offer_expiry():
    """Verify offer expiry is strictly in the future."""
    async def run():
        provider = make_test_provider()
        msg = make_request_message(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=1)

        result = await provider.execute(msg)
        offer = ProviderOffer.model_validate(result.data["offer"])
        assert offer.expires_at > offer.created_at
        assert offer.expires_at.tzinfo == timezone.utc

    asyncio.run(run())


def test_does_not_modify_provider_capacity():
    """Invariant test: evaluating an offer must not deduct or alter the provider's inventory."""
    async def run():
        provider = make_test_provider()
        inv_before = copy.deepcopy(provider.inventory)
        msg = make_request_message(resource_type=ResourceType.DIAGNOSTIC_SLOT, quantity=3)

        await provider.execute(msg)
        assert provider.inventory == inv_before

    asyncio.run(run())


def test_does_not_expose_patient_sensitive_information():
    """Verify PII in the request payload triggers a privacy violation failure."""
    async def run():
        provider = make_test_provider()
        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="ProviderAgent_lab-c",
            message_type="REQUEST_OFFER",
            payload={
                "resource_request": {
                    "requester_institution_id": "hospital-a",
                    "resource_type": "diagnostic_slot",
                    "quantity": 1,
                    "location": {"city": "Metropolis"},
                    "required_by": (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat(),
                    "patient_name": "Classified Identity",
                }
            },
        )

        result = await provider.execute(msg)
        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "Privacy violation" in result.error.message

    asyncio.run(run())


def test_inventory_isolation_between_providers():
    """Verify each ProviderAgent maintains strict data isolation from other providers."""
    async def run():
        provider_c = make_test_provider(provider_id="lab-c", inventory={"diagnostic_slot": {"total": 12, "available": 8}})
        provider_d = make_test_provider(provider_id="blood-bank-d", inventory={"blood_unit": {"total": 50, "available": 25}})

        assert "blood_unit" not in provider_c.inventory
        assert "diagnostic_slot" not in provider_d.inventory

        # Mutating one does not affect the other
        provider_c.inventory["diagnostic_slot"]["available"] = 5
        assert provider_c.inventory["diagnostic_slot"]["available"] == 5
        assert "diagnostic_slot" not in provider_d.inventory

    asyncio.run(run())
