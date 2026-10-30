from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest

from app.schemas.provider_offer import ProviderCost
from app.schemas.negotiation import NegotiationProposal, NegotiationStatus


def test_valid_negotiation_proposal_creation():
    """Verify standard NegotiationProposal creation covering all 11 concepts."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=2)

    workflow_id = uuid4()
    request_id = uuid4()
    offer_id = uuid4()

    prop = NegotiationProposal(
        workflow_id=workflow_id,
        request_id=request_id,
        provider_offer_id=offer_id,
        proposed_quantity=4,
        proposed_cost=ProviderCost(amount=110.0, currency="USD", is_negotiable=False),
        proposed_conditions=["Fasting required", "Expedited 2hr turnaround"],
        proposed_specifications={"blood_group": "A_POS"},
        proposed_by="hospital-a",
        counterparty_id="lab-c",
        created_at=now,
        expires_at=expiry,
        status=NegotiationStatus.PENDING,
        rationale="Requested 10% volume discount for 4 diagnostic slots.",
    )

    assert isinstance(prop.negotiation_id, UUID)
    assert prop.workflow_id == workflow_id
    assert prop.request_id == request_id
    assert prop.provider_offer_id == offer_id
    assert prop.proposed_quantity == 4
    assert prop.proposed_cost.amount == 110.0
    assert prop.proposed_by == "hospital-a"
    assert prop.counterparty_id == "lab-c"
    assert prop.status == NegotiationStatus.PENDING
    assert prop.round_number == 1
    assert prop.previous_proposal_id is None


def test_negotiation_statuses():
    """Verify all statuses PENDING, ACCEPTED, REJECTED, COUNTERED, EXPIRED."""
    for s in ["PENDING", "ACCEPTED", "REJECTED", "COUNTERED", "EXPIRED"]:
        enum_val = NegotiationStatus(s)
        assert enum_val.value == s

    with pytest.raises(ValueError):
        NegotiationStatus("AUTO_BOOKED")  # Negotiation cannot bypass smart contract booking


def test_reject_negative_or_zero_quantity():
    """Verify proposed_quantity must be strictly positive."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=1)

    with pytest.raises(ValueError):
        NegotiationProposal(
            workflow_id=uuid4(),
            request_id=uuid4(),
            provider_offer_id=uuid4(),
            proposed_quantity=0,  # <= 0
            proposed_by="hospital-a",
            expires_at=expiry,
        )

    with pytest.raises(ValueError):
        NegotiationProposal(
            workflow_id=uuid4(),
            request_id=uuid4(),
            provider_offer_id=uuid4(),
            proposed_quantity=-5,  # < 0
            proposed_by="hospital-a",
            expires_at=expiry,
        )


def test_reject_negative_cost():
    """Verify proposed_cost cannot be negative."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=1)

    with pytest.raises(ValueError):
        NegotiationProposal(
            workflow_id=uuid4(),
            request_id=uuid4(),
            provider_offer_id=uuid4(),
            proposed_quantity=2,
            proposed_cost=ProviderCost(amount=-20.0, currency="USD"),
            proposed_by="hospital-a",
            expires_at=expiry,
        )


def test_expiry_must_be_later_than_creation():
    """Verify expires_at <= created_at is strictly rejected."""
    now = datetime.now(timezone.utc)

    # 1. Exactly equal
    with pytest.raises(ValueError, match="expires_at must be strictly later than created_at"):
        NegotiationProposal(
            workflow_id=uuid4(),
            request_id=uuid4(),
            provider_offer_id=uuid4(),
            proposed_quantity=1,
            proposed_by="hospital-a",
            created_at=now,
            expires_at=now,
        )

    # 2. In the past
    with pytest.raises(ValueError, match="expires_at must be strictly later than created_at"):
        NegotiationProposal(
            workflow_id=uuid4(),
            request_id=uuid4(),
            provider_offer_id=uuid4(),
            proposed_quantity=1,
            proposed_by="hospital-a",
            created_at=now,
            expires_at=now - timedelta(minutes=10),
        )


def test_counteroffer_lineage_preserves_original():
    """Verify counteroffer tracks round_number and previous_proposal_id without mutating original."""
    now = datetime.now(timezone.utc)
    expiry1 = now + timedelta(hours=2)
    offer_id = uuid4()
    req_id = uuid4()
    wf_id = uuid4()

    # Turn 1: Requester proposes $90
    turn1 = NegotiationProposal(
        workflow_id=wf_id,
        request_id=req_id,
        provider_offer_id=offer_id,
        proposed_quantity=2,
        proposed_cost=ProviderCost(amount=90.0, currency="USD"),
        proposed_by="hospital-a",
        counterparty_id="lab-c",
        created_at=now,
        expires_at=expiry1,
        status=NegotiationStatus.PENDING,
        round_number=1,
    )

    # Turn 2: Provider counters with $95
    expiry2 = now + timedelta(hours=3)
    turn2 = NegotiationProposal(
        workflow_id=wf_id,
        request_id=req_id,
        provider_offer_id=offer_id,
        previous_proposal_id=turn1.negotiation_id,
        proposed_quantity=2,
        proposed_cost=ProviderCost(amount=95.0, currency="USD"),
        proposed_by="lab-c",
        counterparty_id="hospital-a",
        created_at=now + timedelta(minutes=15),
        expires_at=expiry2,
        status=NegotiationStatus.COUNTERED,
        round_number=2,
        rationale="Cannot do $90, offering $95 with express priority.",
    )

    # Verify both records exist independently
    assert turn1.negotiation_id != turn2.negotiation_id
    assert turn2.previous_proposal_id == turn1.negotiation_id
    assert turn1.proposed_cost.amount == 90.0  # Turn 1 unchanged
    assert turn2.proposed_cost.amount == 95.0
    assert turn2.round_number == 2


def test_negotiation_rejects_pii():
    """Verify strict rejection of any patient identifying attributes."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=1)

    forbidden_payloads = [
        {"patient_name": "Alice Smith"},
        {"patient_id": "PT-1234"},
        {"mrn": "MRN-001"},
        {"ssn": "999-00-1111"},
    ]

    for pii in forbidden_payloads:
        payload = {
            "workflow_id": str(uuid4()),
            "request_id": str(uuid4()),
            "provider_offer_id": str(uuid4()),
            "proposed_quantity": 1,
            "proposed_by": "hospital-a",
            "created_at": now.isoformat(),
            "expires_at": expiry.isoformat(),
            **pii,
        }
        with pytest.raises(ValueError, match="Privacy violation: patient identifying fields are strictly forbidden"):
            NegotiationProposal.model_validate(payload)


def test_json_roundtrip_serialization():
    """Verify JSON serialization roundtrip."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(hours=1)

    prop = NegotiationProposal(
        workflow_id=uuid4(),
        request_id=uuid4(),
        provider_offer_id=uuid4(),
        proposed_quantity=5,
        proposed_cost=ProviderCost(amount=200.0, currency="USD"),
        proposed_by="clinic-x",
        created_at=now,
        expires_at=expiry,
        status=NegotiationStatus.ACCEPTED,
    )

    json_data = prop.model_dump_json()
    assert "ACCEPTED" in json_data
    assert "clinic-x" in json_data

    restored = NegotiationProposal.model_validate_json(json_data)
    assert restored.negotiation_id == prop.negotiation_id
    assert restored.status == NegotiationStatus.ACCEPTED
    assert restored.proposed_cost.amount == 200.0

