from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict

from .resource_request import LocationArea, ResourceType


class OfferStatus(str, Enum):
    """
    Lifecycle status of a provider offer.
    
    Architectural Invariant:
    An offer is strictly a discovery proposal, NOT a reservation or capacity commitment.
    Capacity must be locked on-chain (LOCKED) and subsequently converted (CONVERTED)
    via the smart contract before any commitment is final.
    """
    OFFERED = "offered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    LOCKED = "locked"
    CONVERTED = "converted"
    CANCELLED = "cancelled"


class ProviderCost(BaseModel):
    """Estimated financial cost or reimbursement structure for the offered resource."""
    model_config = ConfigDict(extra="ignore")

    amount: float = Field(..., ge=0.0, description="Financial amount (must be non-negative)")
    currency: str = Field(default="USD", min_length=1, max_length=10, description="Currency or token symbol")
    is_negotiable: bool = Field(default=False, description="Whether the rate can be negotiated by agents")
    breakdown: dict[str, float] = Field(default_factory=dict, description="Itemized fee breakdown")

    @field_validator("currency")
    @classmethod
    def clean_currency(cls, v: str) -> str:
        stripped = v.strip().upper()
        if not stripped:
            raise ValueError("Currency cannot be empty or whitespace only")
        return stripped


class ProviderOffer(BaseModel):
    """
    Standardized offer returned by provider agents (hospitals, labs, blood banks, etc.).
    
    Important Distinction:
    An offer is NOT a reservation. Capacity must be checked against on-chain smart
    contracts and temporarily locked before becoming a confirmed commitment.
    """
    model_config = ConfigDict(extra="ignore")

    offer_id: UUID = Field(default_factory=uuid4, description="Unique offer identifier")
    request_id: UUID = Field(..., description="ID of the originating ResourceRequest")
    workflow_id: Optional[UUID] = Field(default=None, description="Multi-agent workflow identifier")

    # 1. Provider and institution identifiers
    provider_id: str = Field(..., min_length=1, description="Registered institutional identifier (e.g. 'lab-c')")
    provider_name: str = Field(..., min_length=1, description="Human-readable provider name")
    institution_type: Optional[str] = Field(
        default=None,
        description="Category: 'hospital', 'diagnostic_lab', 'blood_bank', 'pharmacy', 'ambulance_service'",
    )

    # 2. Offered resource type and quantity
    resource_type: ResourceType = Field(..., description="Category of resource being offered")
    offered_quantity: int = Field(..., gt=0, le=10000, description="Offered capacity quantity")

    # 3. Location or service area
    location: Optional[LocationArea] = Field(
        default=None,
        description="Provider facility location or logistical service area",
    )

    # 4. Availability start and end times
    available_from: datetime = Field(
        ...,
        description="Earliest time this capacity can be utilized (UTC, timezone-aware)",
    )
    available_until: datetime = Field(
        ...,
        description="Latest time this capacity remains available (UTC, timezone-aware)",
    )

    # 5. Price or estimated cost, if applicable
    cost: Optional[ProviderCost] = Field(
        default=None,
        description="Optional financial or reimbursement details",
    )

    # 6. Offer expiry time
    expires_at: datetime = Field(
        ...,
        description="Timestamp after which this offer is void if unaccepted (UTC, timezone-aware)",
    )

    # 7. Constraints and conditions
    conditions: list[str] = Field(
        default_factory=list,
        description="Operational requirements (e.g., 'Requires refrigerated transfer', 'Fasting required')",
    )
    specifications: dict[str, Any] = Field(
        default_factory=dict,
        description="Technical specs of offered resource (e.g., {'blood_group': 'O_NEG', 'make': 'Siemens 3T'})",
    )

    # 8. Provider offer status
    status: OfferStatus = Field(
        default=OfferStatus.OFFERED,
        description="Current lifecycle state of this offer",
    )

    # Operational & Blockchain verification
    estimated_delivery_hours: Optional[int] = Field(
        default=None,
        ge=0,
        le=720,
        description="Estimated fulfillment or delivery time in hours",
    )
    onchain_verified: bool = Field(
        default=False,
        description="Whether capacity was verified on Coral Testnet smart contract",
    )
    lock_id: Optional[str] = Field(
        default=None,
        description="On-chain 2PC lock ID if capacity has been temporarily locked",
    )
    rationale: Optional[str] = Field(
        default=None,
        description="Matching rationale or explanation from the provider agent",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Offer generation timestamp (UTC, timezone-aware)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional coordination metadata",
    )

    @field_validator("provider_id", "provider_name")
    @classmethod
    def validate_identifiers(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Provider identifiers cannot be empty or whitespace only")
        return stripped

    @field_validator("available_from", "available_until", "expires_at", "created_at")
    @classmethod
    def validate_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("Timestamps must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def validate_timelines(self) -> "ProviderOffer":
        """Ensures temporal consistency across availability and expiry."""
        if self.available_until < self.available_from:
            raise ValueError("available_until cannot be earlier than available_from")
        if self.expires_at < self.created_at:
            raise ValueError("expires_at cannot be in the past relative to created_at")
        return self

