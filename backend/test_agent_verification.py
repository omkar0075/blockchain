import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4
import pytest

from app.agents.verification_agent import (
    DEFAULT_AUTHORITATIVE_INVENTORY,
    DEFAULT_INSTITUTIONAL_REGISTRY,
    VerificationAgent,
)
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
from app.schemas.verification import (
    VerificationChecklist,
    VerificationResult,
    VerificationStatus,
)


def make_request(
    request_id: Optional[UUID] = None,
    quantity: int = 2,
    city: str = "Metropolis",
    resource_type: ResourceType = ResourceType.DIAGNOSTIC_SLOT,
) -> ResourceRequest:
    now = datetime.now(timezone.utc)
    return ResourceRequest(
        request_id=request_id or uuid4(),
        requester_institution_id="hospital-a",
        resource_type=resource_type,
        quantity=quantity,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city=city),
        required_by=now + timedelta(hours=12),
        required_by_hours=12,
    )


def make_offer(
    request: ResourceRequest,
    provider_id: str = "lab-c",
    offered_quantity: int = 2,
    resource_type: Optional[ResourceType] = None,
    city: str = "Metropolis",
    is_expired: bool = False,
    operational_wallet: Optional[str] = None,
) -> ProviderOffer:
    now = datetime.now(timezone.utc)
    if is_expired:
        created = now - timedelta(hours=5)
        expiry = now - timedelta(hours=1)
    else:
        created = now
        expiry = now + timedelta(hours=6)

    meta = {}
    if operational_wallet:
        meta["operational_wallet"] = operational_wallet

    return ProviderOffer(
        request_id=request.request_id,
        provider_id=provider_id,
        provider_name=f"Provider {provider_id}",
        resource_type=resource_type or request.resource_type,
        offered_quantity=offered_quantity,
        location=LocationArea(city=city),
        available_from=created,
        available_until=created + timedelta(days=2),
        created_at=created,
        expires_at=expiry,
        estimated_delivery_hours=2,
        cost=ProviderCost(amount=100.0, currency="USD"),
        status=OfferStatus.OFFERED,
        metadata=meta,
    )


def make_verification_message(
    request: ResourceRequest,
    offer: ProviderOffer,
    authoritative_capacity: Optional[int | str] = None,
    operational_wallet: Optional[str] = None,
) -> AgentMessage:
    payload = {
        "resource_request": request.model_dump(),
        "provider_offer": offer.model_dump(),
    }
    if authoritative_capacity is not None:
        payload["authoritative_capacity"] = authoritative_capacity
    if operational_wallet is not None:
        payload["operational_wallet"] = operational_wallet

    return AgentMessage(
        workflow_id=uuid4(),
        sender="Orchestrator",
        receiver="VerificationAgent",
        message_type="VERIFY_OFFER",
        payload=payload,
    )


def test_verifies_valid_provider_and_offer():
    """Verify that a valid active provider, authorized resource, matching offer, and valid capacity pass."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        # lab-c is registered in Metropolis, active, authorized for diagnostic_slot, with registered wallet
        offer = make_offer(req, provider_id="lab-c", offered_quantity=2)

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        assert isinstance(result, AgentResult)
        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is True
        assert result.data["status"] == "VERIFIED"

        checks = result.data["checks"]
        assert checks["institution_registered"] is True
        assert checks["institution_active"] is True
        assert checks["resource_registered"] is True
        assert checks["wallet_authorized"] is True
        assert checks["request_compatible"] is True
        assert checks["offer_unexpired"] is True
        assert checks["capacity_available"] is True

        assert len(result.data["failed_checks"]) == 0

    asyncio.run(run())


def test_rejects_unregistered_provider():
    """Verify that a provider not present in the institutional registry is rejected."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        offer = make_offer(req, provider_id="unknown-provider-999")

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        assert result.data["success"] is False
        assert result.data["status"] == "REJECTED"
        assert result.data["checks"]["institution_registered"] is False

        reason_codes = [fc["reason_code"] for fc in result.data["failed_checks"]]
        assert "UNREGISTERED_PROVIDER" in reason_codes

    asyncio.run(run())


def test_rejects_inactive_institution():
    """Verify that an inactive/suspended institution is rejected."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        # inactive-clinic-x is registered but is_active=False
        offer = make_offer(req, provider_id="inactive-clinic-x")

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        assert result.data["success"] is False
        assert result.data["status"] == "REJECTED"
        assert result.data["checks"]["institution_active"] is False

        reason_codes = [fc["reason_code"] for fc in result.data["failed_checks"]]
        assert "INACTIVE_INSTITUTION" in reason_codes

    asyncio.run(run())


def test_rejects_unauthorized_resource_type():
    """Verify that an institution offering a resource type outside its accreditation is rejected."""
    async def run():
        agent = VerificationAgent()
        # lab-c is only accredited for diagnostic_slot, NOT blood_unit
        req = make_request(resource_type=ResourceType.BLOOD_UNIT)
        offer = make_offer(req, provider_id="lab-c", resource_type=ResourceType.BLOOD_UNIT)

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        assert result.data["success"] is False
        assert result.data["status"] == "REJECTED"
        assert result.data["checks"]["resource_registered"] is False

        reason_codes = [fc["reason_code"] for fc in result.data["failed_checks"]]
        assert "UNAUTHORIZED_RESOURCE_TYPE" in reason_codes

    asyncio.run(run())


def test_detects_wallet_mismatch():
    """Verify that an offer signed or submitted with an unapproved wallet is detected and rejected."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        # Spoofed operational wallet
        offer = make_offer(
            req,
            provider_id="lab-c",
            operational_wallet="0x000000000000000000000000000000000000dead",
        )

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        assert result.data["success"] is False
        assert result.data["status"] == "REJECTED"
        assert result.data["checks"]["wallet_authorized"] is False

        reason_codes = [fc["reason_code"] for fc in result.data["failed_checks"]]
        assert "WALLET_MISMATCH" in reason_codes

    asyncio.run(run())


def test_rejects_expired_offers():
    """Verify that an expired offer is rejected."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        offer = make_offer(req, provider_id="lab-c", is_expired=True)

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        assert result.data["success"] is False
        assert result.data["status"] == "REJECTED"
        assert result.data["checks"]["offer_unexpired"] is False

        reason_codes = [fc["reason_code"] for fc in result.data["failed_checks"]]
        assert "OFFER_EXPIRED" in reason_codes

    asyncio.run(run())


def test_detects_request_offer_mismatch():
    """Verify that mismatches between request parameters and offer parameters are caught."""
    async def run():
        agent = VerificationAgent()
        req = make_request(city="Metropolis")
        # Offer located in Gotham (incompatible location)
        offer = make_offer(req, provider_id="lab-c", city="Gotham")

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        assert result.data["success"] is False
        assert result.data["status"] == "REJECTED"
        assert result.data["checks"]["request_compatible"] is False

        reason_codes = [fc["reason_code"] for fc in result.data["failed_checks"]]
        assert "LOCATION_MISMATCH" in reason_codes

    asyncio.run(run())


def test_returns_requires_review_when_authoritative_data_unavailable():
    """Verify that when authoritative capacity is unreachable or ambiguous, REQUIRES_REVIEW is returned."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        offer = make_offer(req, provider_id="lab-c")

        # Explicitly pass UNAVAILABLE authoritative capacity
        msg = make_verification_message(req, offer, authoritative_capacity="UNAVAILABLE")
        result = await agent.execute(msg)

        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["status"] == "REQUIRES_REVIEW"
        assert result.data["checks"]["capacity_available"] is False

        reason_codes = [fc["reason_code"] for fc in result.data["failed_checks"]]
        assert "AUTHORITATIVE_DATA_UNAVAILABLE" in reason_codes

    asyncio.run(run())


def test_does_not_claim_capacity_is_reserved():
    """Governance Invariant: Verification must never claim capacity is reserved or execute reservations."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        offer = make_offer(req, provider_id="lab-c", offered_quantity=2)

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        # Invariant checks:
        assert result.data["capacity_reserved"] is False
        assert result.data["verification"]["metadata"]["capacity_reserved"] is False
        assert result.data["verification"]["metadata"]["requires_onchain_lock"] is True
        assert "unreserved" in result.data["explanation"].lower() or "pending" in result.data["explanation"].lower()

    asyncio.run(run())


def test_returns_structured_verification_result():
    """Verify that the verification output conforms strictly to the VerificationResult schema."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        offer = make_offer(req, provider_id="lab-c")

        msg = make_verification_message(req, offer)
        result = await agent.execute(msg)

        v_raw = result.data["verification"]
        # Validate directly through Pydantic v2 schema
        verification_model = VerificationResult.model_validate(v_raw)

        assert isinstance(verification_model.verification_id, UUID)
        assert verification_model.status == VerificationStatus.VERIFIED
        assert isinstance(verification_model.checks, VerificationChecklist)
        assert verification_model.checks.is_all_passed() is True
        assert verification_model.failed_checks == []

    asyncio.run(run())


def test_rejects_pii_in_payload():
    """Privacy Invariant: Verification Agent rejects any patient identifying data in message payload."""
    async def run():
        agent = VerificationAgent()
        req = make_request()
        offer = make_offer(req, provider_id="lab-c")

        msg = AgentMessage(
            workflow_id=uuid4(),
            sender="Orchestrator",
            receiver="VerificationAgent",
            message_type="VERIFY_OFFER",
            payload={
                "resource_request": req.model_dump(),
                "provider_offer": offer.model_dump(),
                "patient_name": "John Doe",  # Forbidden PII
            },
        )
        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "patient identifying" in result.error.message.lower()

    asyncio.run(run())
