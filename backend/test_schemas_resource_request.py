from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest

from app.schemas.resource_request import (
    GeoCoordinates,
    LocationArea,
    ResourceConstraints,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)


def test_valid_resource_request_creation():
    """Verify that a standard ResourceRequest can be instantiated with valid parameters."""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=12)

    req = ResourceRequest(
        requester_institution_id="hospital-a",
        requester_institution_name="Apollo Hospital Metro",
        resource_type=ResourceType.ICU_BED,
        quantity=3,
        urgency=UrgencyLevel.CRITICAL,
        location=LocationArea(
            city="Metropolis",
            region="State Central",
            postal_code="560001",
            coordinates=GeoCoordinates(latitude=12.9716, longitude=77.5946),
            max_radius_km=30.0,
        ),
        required_by=deadline,
        required_by_hours=12,
        constraints=ResourceConstraints(
            specifications={"negative_pressure": True, "ventilator_supported": True},
            preferred_providers=["hospital-b", "lab-c"],
            excluded_providers=["hospital-z"],
            accreditations_required=["NABH"],
        ),
    )

    assert isinstance(req.request_id, UUID)
    assert req.requester_institution_id == "hospital-a"
    assert req.resource_type == ResourceType.ICU_BED
    assert req.quantity == 3
    assert req.urgency == UrgencyLevel.CRITICAL
    assert req.location.city == "Metropolis"
    assert req.location.coordinates.latitude == 12.9716
    assert req.constraints.specifications["negative_pressure"] is True
    assert req.created_at.tzinfo == timezone.utc


def test_all_resource_types_and_urgency_levels():
    """Verify all defined resource categories and urgency levels are recognized."""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=6)

    expected_types = [
        "hospital_bed",
        "icu_bed",
        "blood_unit",
        "diagnostic_slot",
        "ambulance",
        "medicine",
        "oxygen_cylinder",
        "ventilator",
    ]

    for rt in expected_types:
        req = ResourceRequest(
            requester_institution_id="clinic-1",
            resource_type=ResourceType(rt),
            quantity=1,
            location=LocationArea(city="Springfield"),
            required_by=deadline,
        )
        assert req.resource_type.value == rt

    for u in ["routine", "urgent", "critical"]:
        req = ResourceRequest(
            requester_institution_id="clinic-1",
            resource_type=ResourceType.MEDICINE,
            quantity=5,
            urgency=UrgencyLevel(u),
            location=LocationArea(city="Springfield"),
            required_by=deadline,
        )
        assert req.urgency.value == u


def test_invalid_resource_type_and_urgency_rejected():
    """Verify invalid strings are rejected by enum validation."""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=6)

    with pytest.raises(ValueError):
        ResourceRequest(
            requester_institution_id="clinic-1",
            resource_type="unregistered_cyberware",  # type: ignore
            quantity=1,
            location=LocationArea(city="Springfield"),
            required_by=deadline,
        )

    with pytest.raises(ValueError):
        ResourceRequest(
            requester_institution_id="clinic-1",
            resource_type=ResourceType.AMBULANCE,
            urgency="panic_mode",  # type: ignore
            quantity=1,
            location=LocationArea(city="Springfield"),
            required_by=deadline,
        )


def test_pii_strictly_rejected():
    """Enforce architectural privacy rule: reject patient identifying attributes."""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=6)

    forbidden_inputs = [
        {"patient_name": "John Doe"},
        {"patient_id": "PT-99901"},
        {"ssn": "000-12-3456"},
        {"mrn": "MRN-5544"},
        {"aadhaar": "1234-5678-9012"},
        {"patient_dob": "1980-01-01"},
        {"patient_phone": "+1234567890"},
        {"patient_address": "123 Main St, Apt 4"},
    ]

    for pii_entry in forbidden_inputs:
        payload = {
            "requester_institution_id": "hospital-a",
            "resource_type": "blood_unit",
            "quantity": 2,
            "location": {"city": "Metropolis"},
            "required_by": deadline.isoformat(),
            **pii_entry,
        }
        with pytest.raises(ValueError, match="Privacy violation: patient identifying fields are strictly forbidden"):
            ResourceRequest.model_validate(payload)


def test_timeline_validation():
    """Verify required_by cannot be earlier than created_at."""
    now = datetime.now(timezone.utc)
    past_deadline = now - timedelta(hours=2)

    with pytest.raises(ValueError, match="required_by deadline cannot be earlier than created_at"):
        ResourceRequest(
            requester_institution_id="hospital-a",
            resource_type=ResourceType.OXYGEN_CYLINDER,
            quantity=4,
            location=LocationArea(city="Metropolis"),
            created_at=now,
            required_by=past_deadline,
        )


def test_coordinates_validation():
    """Verify latitude and longitude bounds."""
    with pytest.raises(ValueError):
        GeoCoordinates(latitude=95.0, longitude=10.0)  # > 90

    with pytest.raises(ValueError):
        GeoCoordinates(latitude=10.0, longitude=-190.0)  # < -180

    valid_geo = GeoCoordinates(latitude=40.7128, longitude=-74.0060)
    assert valid_geo.latitude == 40.7128
    assert valid_geo.longitude == -74.0060


def test_json_roundtrip_serialization():
    """Verify ResourceRequest serializes and deserializes to/from JSON."""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=4)

    req = ResourceRequest(
        requester_institution_id="lab-c",
        resource_type=ResourceType.DIAGNOSTIC_SLOT,
        quantity=5,
        urgency=UrgencyLevel.URGENT,
        location=LocationArea(city="Gotham"),
        required_by=deadline,
        constraints=ResourceConstraints(specifications={"scan_type": "MRI_3T"}),
    )

    json_str = req.model_dump_json()
    assert "DIAGNOSTIC_SLOT" not in json_str  # lower-case serialized
    assert "diagnostic_slot" in json_str

    restored = ResourceRequest.model_validate_json(json_str)
    assert restored.request_id == req.request_id
    assert restored.resource_type == ResourceType.DIAGNOSTIC_SLOT
    assert restored.constraints.specifications == {"scan_type": "MRI_3T"}
    assert restored.created_at.tzinfo is not None


def test_flexible_optional_location():
    """Verify location is usable with only postal code, service area, or even omitted."""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=8)

    # 1. Postal code only (no city or GPS)
    loc_postal = LocationArea(postal_code="560001")
    req1 = ResourceRequest(
        requester_institution_id="clinic-a",
        resource_type=ResourceType.AMBULANCE,
        quantity=1,
        location=loc_postal,
        required_by=deadline,
    )
    assert req1.location.postal_code == "560001"
    assert req1.location.city is None
    assert req1.location.coordinates is None

    # 2. Service area string only
    loc_area = LocationArea(service_area="Greater Metro North District")
    req2 = ResourceRequest(
        requester_institution_id="clinic-a",
        resource_type=ResourceType.BLOOD_UNIT,
        quantity=2,
        location=loc_area,
        required_by=deadline,
    )
    assert req2.location.service_area == "Greater Metro North District"

    # 3. Completely empty location area raises ValueError (must have at least one indicator)
    with pytest.raises(ValueError, match="At least one location indicator"):
        LocationArea()

    # 4. Location completely optional on ResourceRequest
    req_no_loc = ResourceRequest(
        requester_institution_id="clinic-b",
        resource_type=ResourceType.MEDICINE,
        quantity=10,
        required_by=deadline,
    )
    assert req_no_loc.location is None


