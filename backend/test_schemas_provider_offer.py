from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest

from app.schemas.resource_request import LocationArea, ResourceType
from app.schemas.provider_offer import OfferStatus, ProviderCost, ProviderOffer


def test_valid_provider_offer_creation():
    """Verify standard ProviderOffer instantiation covering all 8 core attributes."""
    now = datetime.now(timezone.utc)
    from_time = now + timedelta(hours=1)
    until_time = now + timedelta(days=2)
    expiry = now + timedelta(hours=6)
    req_id = uuid4()

    offer = ProviderOffer(
        request_id=req_id,
        provider_id="lab-c",
        provider_name="Lab C Diagnostics",
        institution_type="diagnostic_lab",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        offered_quantity=5,
        location=LocationArea(city="Metropolis", postal_code="560001"),
        available_from=from_time,
        available_until=until_time,
        cost=ProviderCost(
            amount=150.0,
            currency="USD",
            is_negotiable=True,
            breakdown={"base_fee": 120.0, "processing": 30.0},
        ),
        expires_at=expiry,
        conditions=["Fasting 8 hours prior", "Doctor prescription mandatory"],
        specifications={"equipment": "Siemens Magnetom 3T", "contrast_available": True},
        status=OfferStatus.OFFERED,
        estimated_delivery_hours=4,
        onchain_verified=True,
        rationale="Active on-chain verified capacity on Coral Testnet with 5 slots available.",
    )

    assert isinstance(offer.offer_id, UUID)
    assert offer.request_id == req_id
    assert offer.provider_id == "lab-c"
    assert offer.institution_type == "diagnostic_lab"
    assert offer.resource_type == ResourceType.DIAGNOSTIC_SLOT
    assert offer.offered_quantity == 5
    assert offer.cost.amount == 150.0
    assert offer.cost.currency == "USD"
    assert offer.cost.is_negotiable is True
    assert offer.status == OfferStatus.OFFERED
    assert len(offer.conditions) == 2
    assert offer.onchain_verified is True
    assert offer.lock_id is None  # Offer is NOT a reservation yet


def test_offer_status_lifecycle_values():
    """Verify all offer lifecycle statuses are recognized and valid."""
    statuses = [
        "offered",
        "accepted",
        "rejected",
        "expired",
        "locked",
        "converted",
        "cancelled",
    ]
    for s in statuses:
        status_enum = OfferStatus(s)
        assert status_enum.value == s

    with pytest.raises(ValueError):
        OfferStatus("confirmed_reservation")  # Must not confuse offer with confirmed reservation


def test_provider_identifier_validation():
    """Verify empty or whitespace-only provider names and IDs are rejected."""
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=1)
    expiry = now + timedelta(hours=2)

    with pytest.raises(ValueError):
        ProviderOffer(
            request_id=uuid4(),
            provider_id="",
            provider_name="Valid Hospital",
            resource_type=ResourceType.HOSPITAL_BED,
            offered_quantity=1,
            available_from=now,
            available_until=until,
            expires_at=expiry,
        )

    with pytest.raises(ValueError, match="Provider identifiers cannot be empty or whitespace only"):
        ProviderOffer(
            request_id=uuid4(),
            provider_id="hosp-1",
            provider_name="   ",
            resource_type=ResourceType.HOSPITAL_BED,
            offered_quantity=1,
            available_from=now,
            available_until=until,
            expires_at=expiry,
        )


def test_timeline_validation():
    """Verify available_until >= available_from and expires_at >= created_at."""
    now = datetime.now(timezone.utc)

    # 1. available_until before available_from
    with pytest.raises(ValueError, match="available_until cannot be earlier than available_from"):
        ProviderOffer(
            request_id=uuid4(),
            provider_id="hosp-1",
            provider_name="Hospital One",
            resource_type=ResourceType.ICU_BED,
            offered_quantity=1,
            available_from=now + timedelta(hours=10),
            available_until=now + timedelta(hours=5),
            expires_at=now + timedelta(hours=2),
        )

    # 2. expires_at in the past relative to created_at
    with pytest.raises(ValueError, match="expires_at cannot be in the past"):
        ProviderOffer(
            request_id=uuid4(),
            provider_id="hosp-1",
            provider_name="Hospital One",
            resource_type=ResourceType.ICU_BED,
            offered_quantity=1,
            available_from=now,
            available_until=now + timedelta(days=1),
            created_at=now,
            expires_at=now - timedelta(minutes=5),
        )


def test_timezone_awareness_enforced():
    """Verify naive datetimes are rejected."""
    now_naive = datetime.now()
    now_aware = datetime.now(timezone.utc)

    with pytest.raises(ValueError, match="Timestamps must be timezone-aware"):
        ProviderOffer(
            request_id=uuid4(),
            provider_id="hosp-1",
            provider_name="Hospital One",
            resource_type=ResourceType.VENTILATOR,
            offered_quantity=1,
            available_from=now_naive,  # Naive
            available_until=now_aware + timedelta(days=1),
            expires_at=now_aware + timedelta(hours=1),
        )


def test_cost_currency_and_non_negative_validation():
    """Verify cost currency formatting and rejection of negative cost."""
    cost = ProviderCost(amount=0.0, currency="  inr ")
    assert cost.currency == "INR"
    assert cost.amount == 0.0

    with pytest.raises(ValueError):
        ProviderCost(amount=-50.0, currency="USD")


def test_json_roundtrip_serialization():
    """Verify ProviderOffer serializes and deserializes to/from JSON."""
    now = datetime.now(timezone.utc)
    req_id = uuid4()

    offer = ProviderOffer(
        request_id=req_id,
        provider_id="blood-bank-a",
        provider_name="Red Cross Metro Bank",
        institution_type="blood_bank",
        resource_type=ResourceType.BLOOD_UNIT,
        offered_quantity=10,
        available_from=now,
        available_until=now + timedelta(days=3),
        expires_at=now + timedelta(hours=12),
        cost=ProviderCost(amount=25.0, currency="USD"),
        status=OfferStatus.LOCKED,
        lock_id="lock-coral-99881",
        onchain_verified=True,
    )

    json_data = offer.model_dump_json()
    assert "blood-bank-a" in json_data
    assert "lock-coral-99881" in json_data
    assert "locked" in json_data

    restored = ProviderOffer.model_validate_json(json_data)
    assert restored.offer_id == offer.offer_id
    assert restored.request_id == req_id
    assert restored.status == OfferStatus.LOCKED
    assert restored.lock_id == "lock-coral-99881"
    assert restored.cost.amount == 25.0
