from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict


class ResourceType(str, Enum):
    """Supported healthcare resource categories in MEDway coordination network."""
    HOSPITAL_BED = "hospital_bed"
    ICU_BED = "icu_bed"
    BLOOD_UNIT = "blood_unit"
    DIAGNOSTIC_SLOT = "diagnostic_slot"
    AMBULANCE = "ambulance"
    MEDICINE = "medicine"
    OXYGEN_CYLINDER = "oxygen_cylinder"
    VENTILATOR = "ventilator"


class UrgencyLevel(str, Enum):
    """Urgency tier determining matching priority and delivery thresholds."""
    ROUTINE = "routine"
    URGENT = "urgent"
    CRITICAL = "critical"


class GeoCoordinates(BaseModel):
    """Geographic coordinate representation for proximity calculations."""
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude between -90 and 90")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude between -180 and 180")


class LocationArea(BaseModel):
    """
    Service area and geographic routing boundary.
    
    Contains strictly logistical location data without patient address or PII.
    """
    model_config = ConfigDict(extra="ignore")

    city: str = Field(..., min_length=1, description="Primary city or metropolitan area")
    region: Optional[str] = Field(default=None, description="State, province, or district")
    postal_code: Optional[str] = Field(default=None, description="Logistical postal routing code")
    coordinates: Optional[GeoCoordinates] = Field(default=None, description="Centroid or facility coordinates")
    max_radius_km: Optional[float] = Field(
        default=None,
        gt=0.0,
        le=2000.0,
        description="Maximum acceptable search radius in kilometers",
    )

    @field_validator("city")
    @classmethod
    def validate_city(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("City cannot be empty or whitespace only")
        return stripped


class ResourceConstraints(BaseModel):
    """
    Operational constraints for resource matching.
    
    Restricted strictly to clinical resource attributes (e.g. blood group,
    equipment spec) without personal patient identifying data.
    """
    model_config = ConfigDict(extra="ignore")

    specifications: dict[str, Any] = Field(
        default_factory=dict,
        description="Specific technical or biological attributes (e.g. blood_group='O_NEG')",
    )
    preferred_providers: list[str] = Field(
        default_factory=list,
        description="List of accredited provider IDs with preferred relationship",
    )
    excluded_providers: list[str] = Field(
        default_factory=list,
        description="List of provider IDs excluded from consideration",
    )
    accreditations_required: list[str] = Field(
        default_factory=list,
        description="Required certifications (e.g. NABH, CAP, ISO 15189)",
    )


FORBIDDEN_PII_KEYWORDS = {
    "patient_name",
    "patient_id",
    "ssn",
    "mrn",
    "aadhaar",
    "national_id",
    "patient_dob",
    "patient_phone",
    "patient_address",
}


class ResourceRequest(BaseModel):
    """
    Standardized resource request produced by the Request Agent and consumed by Discovery Agents.
    
    Design Invariant:
    Preserves strict patient privacy. Clinical details and patient identities
    are entirely excluded. Contains only logistical, institutional, and capacity requirements.
    """
    model_config = ConfigDict(extra="ignore")

    request_id: UUID = Field(default_factory=uuid4, description="Unique identifier for this resource request")
    workflow_id: Optional[UUID] = Field(default=None, description="Associated multi-agent workflow identifier")
    requester_institution_id: str = Field(..., min_length=1, description="Registered institutional identifier")
    requester_institution_name: Optional[str] = Field(default=None, description="Human-readable hospital or lab name")
    resource_type: ResourceType = Field(..., description="Category of healthcare resource needed")
    quantity: int = Field(..., gt=0, le=10000, description="Number of units/slots requested")
    urgency: UrgencyLevel = Field(default=UrgencyLevel.ROUTINE, description="Urgency priority level")
    location: LocationArea = Field(..., description="Target service area or routing boundaries")
    required_by: datetime = Field(
        ...,
        description="Target deadline timestamp for resource availability (UTC, timezone-aware)",
    )
    required_by_hours: Optional[int] = Field(
        default=None,
        ge=1,
        le=720,
        description="Relative time limit in hours (1-720)",
    )
    constraints: Optional[ResourceConstraints] = Field(
        default=None,
        description="Optional matching constraints and specifications",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Request submission timestamp (UTC, timezone-aware)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Operational coordination metadata",
    )

    @field_validator("requester_institution_id")
    @classmethod
    def validate_institution_id(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("requester_institution_id cannot be empty or whitespace only")
        return stripped

    @field_validator("required_by", "created_at")
    @classmethod
    def validate_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("Timestamp must be timezone-aware (UTC)")
        return v

    @model_validator(mode="before")
    @classmethod
    def reject_pii(cls, values: Any) -> Any:
        """Enforces design principle: reject any attempt to include patient PII."""
        if isinstance(values, dict):
            keys = set(values.keys())
            found_pii = keys.intersection(FORBIDDEN_PII_KEYWORDS)
            if found_pii:
                raise ValueError(
                    f"Privacy violation: patient identifying fields are strictly forbidden ({', '.join(sorted(found_pii))})"
                )
        return values

    @model_validator(mode="after")
    def validate_timeline(self) -> "ResourceRequest":
        """Ensures the required_by deadline is not set prior to the created_at time."""
        if self.required_by < self.created_at:
            raise ValueError("required_by deadline cannot be earlier than created_at timestamp")
        return self
