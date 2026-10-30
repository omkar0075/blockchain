from typing import Literal, Optional
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
    onchain_verified: bool = False

class AllocationProposal(BaseModel):
    proposal_id: str | None = None
    request: ResourceRequest
    offers: list[AllocationOffer]
    selected_provider_id: str | None = None
    selected_quantity: int = 0
    requires_authorization: bool = True
    status: Literal["proposed", "authorized", "committed", "fulfilled", "cancelled", "disputed"] = "proposed"
    lock_id: Optional[str] = None
    lock_status: Optional[str] = None
    lock_expires_at: Optional[str] = None
    lock_tx_hash: Optional[str] = None

class AuthorizationRequest(BaseModel):
    approved_by: str = Field(min_length=2, max_length=100)
    approved: bool

class InventoryUpdateRequest(BaseModel):
    provider_id: str = Field(default="lab-c", description="Accredited provider institution identifier")
    resource_type: ResourceType = "diagnostic_slot"
    total_capacity: int = Field(ge=0, le=10000, description="Total diagnostic capacity quota")
    is_active: bool = True

class CapacityLockReleaseRequest(BaseModel):
    reason: str = Field(default="manual-release", min_length=3, max_length=200)

class DisputeRequest(BaseModel):
    disputed_by: str = Field(default="hospital-a", description="Institution raising the dispute")
    reason: str = Field(min_length=5, max_length=500, description="Off-chain clinical or operational dispute rationale")
    evidence_hash: Optional[str] = Field(default=None, description="Optional 32-byte hex hash of off-chain incident evidence")

class ResolveDisputeRequest(BaseModel):
    resolution: Literal["Fulfilled", "Cancelled"] = Field(description="Arbitration outcome on Coral Testnet")
    notes: str = Field(min_length=5, max_length=500, description="Arbitration findings and resolution rationale")
    resolution_evidence: Optional[str] = Field(default=None, description="Optional 32-byte hex hash of formal arbitration record")
