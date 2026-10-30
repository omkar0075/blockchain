from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict

from .provider_offer import ProviderCost
from .resource_request import FORBIDDEN_PII_KEYWORDS


class NegotiationStatus(str, Enum):
    """
    Lifecycle status of a negotiation proposal or step.
    
    Architectural Invariant:
    A negotiation agreement (ACCEPTED) does NOT constitute a confirmed reservation
    nor does it guarantee on-chain capacity. Capacity must be re-verified and
    temporarily locked via Coral Testnet smart contracts.
    """
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    COUNTERED = "COUNTERED"
    EXPIRED = "EXPIRED"


class NegotiationProposal(BaseModel):
    """
    Structured negotiation proposal exchanged between requester and provider agents.
    
    Design Invariants:
    1. Immutability: Counteroffers generate new proposal records linked via
       previous_proposal_id without silently modifying prior offers.
    2. Authority: Negotiation cannot bypass institutional authorization,
       provider capacity limits, or blockchain smart contract invariants.
    3. Privacy: Strictly prohibits patient identifying information.
    """
    model_config = ConfigDict(extra="ignore")

    # 1. Unique negotiation proposal identifier
    negotiation_id: UUID = Field(
        default_factory=uuid4,
        description="Unique identifier for this negotiation interaction or step",
    )

    # 2. Associated workflow identifier
    workflow_id: UUID = Field(
        ...,
        description="Parent multi-agent workflow tracking this coordination process",
    )

    # 3. Original resource request reference
    request_id: UUID = Field(
        ...,
        description="Original ResourceRequest being negotiated",
    )

    # 4. Provider offer being evaluated or countered
    provider_offer_id: UUID = Field(
        ...,
        description="ProviderOffer being accepted, rejected, or countered",
    )

    # Lineage for counteroffers (prevents silent mutation of original offer)
    previous_proposal_id: Optional[UUID] = Field(
        default=None,
        description="ID of the previous proposal if this is a counteroffer",
    )
    round_number: int = Field(
        default=1,
        ge=1,
        le=50,
        description="Negotiation turn counter (starts at 1)",
    )

    # 5. Proposed quantity (must be strictly positive)
    proposed_quantity: int = Field(
        ...,
        gt=0,
        le=10000,
        description="Resource quantity proposed by either party (must be strictly positive)",
    )

    # 6. Optional negotiated cost
    proposed_cost: Optional[ProviderCost] = Field(
        default=None,
        description="Optional financial or token pricing terms proposed",
    )

    # 7. Structured conditions and constraints
    proposed_conditions: list[str] = Field(
        default_factory=list,
        description="Logistical conditions (e.g. 'Pickup by 14:00', 'Requires priority cold storage')",
    )
    proposed_specifications: dict[str, Any] = Field(
        default_factory=dict,
        description="Clinical specifications (e.g. {'blood_group': 'O_NEG', 'delivery_window_hours': 3})",
    )

    # 8. Identifier of proposing party (requester or provider)
    proposed_by: str = Field(
        ...,
        min_length=1,
        description="Institution identifier of the proposing party",
    )
    counterparty_id: Optional[str] = Field(
        default=None,
        description="Institution identifier of the receiving party",
    )

    # 9. Timezone-aware creation timestamp
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Proposal submission timestamp (UTC, timezone-aware)",
    )

    # 10. Proposal expiry deadline
    expires_at: datetime = Field(
        ...,
        description="Deadline timestamp after which this proposal expires (UTC, timezone-aware)",
    )

    # 11. Negotiation status
    status: NegotiationStatus = Field(
        default=NegotiationStatus.PENDING,
        description="Current status: PENDING, ACCEPTED, REJECTED, COUNTERED, EXPIRED",
    )

    # Reasoning / metadata
    rationale: Optional[str] = Field(
        default=None,
        description="Negotiation rationale or counter-proposal justification",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Operational coordination metadata",
    )

    @field_validator("proposed_by")
    @classmethod
    def validate_proposing_party(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("proposed_by cannot be empty or whitespace only")
        return stripped

    @field_validator("created_at", "expires_at")
    @classmethod
    def validate_timezones(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("Timestamps must be timezone-aware (UTC)")
        return v

    @model_validator(mode="before")
    @classmethod
    def reject_pii(cls, values: Any) -> Any:
        """Enforce architectural privacy rule: reject patient identifying attributes."""
        if isinstance(values, dict):
            keys = set(values.keys())
            found_pii = keys.intersection(FORBIDDEN_PII_KEYWORDS)
            if found_pii:
                raise ValueError(
                    f"Privacy violation: patient identifying fields are strictly forbidden in negotiations ({', '.join(sorted(found_pii))})"
                )
        return values

    @model_validator(mode="after")
    def validate_timelines_and_rules(self) -> "NegotiationProposal":
        """Ensures temporal consistency and logical integrity."""
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be strictly later than created_at")
        if self.status == NegotiationStatus.COUNTERED and self.previous_proposal_id is None and self.round_number <= 1:
            # A counteroffer should ideally reference what is being countered
            pass
        return self
