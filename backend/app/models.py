from typing import Literal
from pydantic import BaseModel, Field

ResourceType = Literal["diagnostic_slot"]

class ResourceRequest(BaseModel):
    requesting_institution_id: str
    resource_type: ResourceType = "diagnostic_slot"
    quantity: int = Field(gt=0, le=100)
    urgency: Literal["routine", "urgent", "critical"] = "routine"
    required_by_hours: int = Field(default=24, ge=1, le=720)

class AllocationOffer(BaseModel):
    provider_id: str
    provider_name: str
    resource_type: ResourceType
    available_quantity: int
    estimated_delivery_hours: int
    rationale: str

class AllocationProposal(BaseModel):
    proposal_id: str | None = None
    request: ResourceRequest
    offers: list[AllocationOffer]
    selected_provider_id: str | None = None
    selected_quantity: int = 0
    requires_authorization: bool = True
    status: Literal["proposed", "authorized", "committed", "fulfilled", "cancelled", "disputed"] = "proposed"

class AuthorizationRequest(BaseModel):
    approved_by: str = Field(min_length=2, max_length=100)
    approved: bool
