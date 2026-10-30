import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from .base import BaseAgent
from ..schemas.agent_messages import AgentMessage, AgentStatus
from ..schemas.provider_offer import OfferStatus, ProviderCost, ProviderOffer
from ..schemas.resource_request import (
    FORBIDDEN_PII_KEYWORDS,
    LocationArea,
    ResourceRequest,
    ResourceType,
)

logger = logging.getLogger("medway.agents.provider")


class ProviderAgent(BaseAgent):
    """
    Autonomous Provider Agent representing an individual healthcare institution.
    
    Responsibilities:
    - Evaluates incoming ResourceRequests against its own private inventory.
    - Generates standardized ProviderOffers.
    - Rejects unserviceable, inactive, or geographically mismatched requests.
    
    Architectural & Security Invariants:
    1. Inventory Privacy & Isolation: Holds its own private inventory. One provider agent
       never accesses another provider's internal state.
    2. Capacity Discipline: NEVER advertises more capacity than its inventory reports.
    3. Zero Transaction Authority: Generating an offer is purely an expression of interest.
       It does NOT lock capacity or execute blockchain transactions.
    4. Zero PII Exposure: Rejects patient PII and ensures no sensitive data leaks into offers.
    """

    def __init__(
        self,
        provider_id: str,
        provider_name: str,
        institution_type: str = "hospital",
        location: Optional[LocationArea] = None,
        is_active: bool = True,
        inventory: Optional[dict[str, dict[str, int]]] = None,
        pricing: Optional[dict[str, ProviderCost]] = None,
        delivery_hours: int = 4,
        offer_validity_hours: int = 6,
        name: Optional[str] = None,
    ) -> None:
        agent_name = name or f"ProviderAgent_{provider_id}"
        super().__init__(name=agent_name)

        clean_id = provider_id.strip() if provider_id else ""
        clean_name = provider_name.strip() if provider_name else ""
        if not clean_id or not clean_name:
            raise ValueError("provider_id and provider_name cannot be empty or whitespace only")

        self.provider_id = clean_id
        self.provider_name = clean_name
        self.institution_type = institution_type
        self.location = location or LocationArea(city="Metropolis")
        self.is_active = is_active
        self.inventory: dict[str, dict[str, int]] = copy.deepcopy(inventory or {})
        self.pricing: dict[str, ProviderCost] = copy.deepcopy(pricing or {})
        self.delivery_hours = max(1, delivery_hours)
        self.offer_validity_hours = max(1, offer_validity_hours)

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Evaluate an incoming ResourceRequest against provider capacity and policies.
        """
        payload = message.payload or {}
        req_raw = payload.get("resource_request") or payload.get("request") or payload

        # 1. PII Scan
        if isinstance(req_raw, dict):
            for k in req_raw.keys():
                if str(k).lower() in FORBIDDEN_PII_KEYWORDS:
                    raise ValueError(f"Privacy violation: patient identifying field '{k}' detected")

        # 2. Validate ResourceRequest
        try:
            request = ResourceRequest.model_validate(req_raw)
        except Exception as exc:
            raise ValueError(f"ProviderAgent requires a valid ResourceRequest: {exc}")

        # Invariant: Provider cannot fulfill its own request
        if request.requester_institution_id == self.provider_id:
            return {
                "success": False,
                "eligible": False,
                "provider_id": self.provider_id,
                "reason": "Self-request: provider cannot fulfill request originating from its own institution",
            }

        # 3. Active Status Check
        if not self.is_active:
            logger.info("Provider '%s' is currently inactive", self.provider_id)
            return {
                "success": False,
                "eligible": False,
                "provider_id": self.provider_id,
                "reason": f"Provider '{self.provider_name}' is currently inactive and cannot accept allocations",
            }

        # 4. Exclusion List Check
        if request.constraints and self.provider_id in request.constraints.excluded_providers:
            return {
                "success": False,
                "eligible": False,
                "provider_id": self.provider_id,
                "reason": "Provider is explicitly excluded in request constraints",
            }

        # 5. Resource Support & Capacity Check
        r_type_str = request.resource_type.value if hasattr(request.resource_type, "value") else str(request.resource_type)
        if r_type_str not in self.inventory:
            logger.info("Provider '%s' does not register resource type '%s'", self.provider_id, r_type_str)
            return {
                "success": False,
                "eligible": False,
                "provider_id": self.provider_id,
                "reason": f"Resource type '{r_type_str}' is not supported by {self.provider_name}",
            }

        res_item = self.inventory[r_type_str]
        advertised = res_item.get("total", res_item.get("advertised", 0))
        available = res_item.get("available", advertised)

        # Invariant: Never advertise more capacity than authoritative inventory reports
        if available < request.quantity:
            logger.info(
                "Provider '%s': insufficient capacity (%d available < %d requested)",
                self.provider_id,
                available,
                request.quantity,
            )
            return {
                "success": False,
                "eligible": False,
                "provider_id": self.provider_id,
                "reason": (
                    f"Insufficient available capacity: requested {request.quantity}, "
                    f"but only {available} uncommitted slots available"
                ),
            }

        # 6. Service Area / Geographic Compatibility
        if request.location:
            if not self._is_location_compatible(request.location):
                logger.info("Provider '%s': location mismatch with request", self.provider_id)
                return {
                    "success": False,
                    "eligible": False,
                    "provider_id": self.provider_id,
                    "reason": "Requested delivery area is outside this provider's operational service area",
                }

        # 7. Delivery SLA Calculation
        delivery_time = self.delivery_hours
        if request.urgency == "critical":
            delivery_time = max(1, delivery_time // 2)

        # 8. Generate Structured ProviderOffer
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self.offer_validity_hours)

        cost = self.pricing.get(r_type_str)

        offer = ProviderOffer(
            request_id=request.request_id,
            workflow_id=message.workflow_id,
            provider_id=self.provider_id,
            provider_name=self.provider_name,
            institution_type=self.institution_type,
            resource_type=request.resource_type,
            offered_quantity=request.quantity,
            location=self.location,
            available_from=now,
            available_until=now + timedelta(days=2),
            expires_at=expires_at,
            estimated_delivery_hours=delivery_time,
            cost=cost,
            conditions=["Standard accredited institutional SLA applies"],
            status=OfferStatus.OFFERED,
            onchain_verified=False,  # Not yet locked on blockchain
            rationale=(
                f"Generated offer for {request.quantity} units of {r_type_str} "
                f"based on {available} uncommitted slots in inventory."
            ),
            metadata={
                "advertised_total": advertised,
                "available_slots": available,
            },
        )

        return {
            "success": True,
            "eligible": True,
            "provider_id": self.provider_id,
            "offer": offer.model_dump(),
        }

    def _is_location_compatible(self, req_loc: LocationArea) -> bool:
        """Check compatibility between requested location and provider location."""
        p_city = (self.location.city or "").lower()
        p_postal = (self.location.postal_code or "").lower()
        p_area = (self.location.service_area or "").lower()

        # If both specify cities, they must be compatible
        if req_loc.city and p_city and req_loc.city.lower() not in p_city and p_city not in req_loc.city.lower():
            return False

        # If both specify postal codes, they must match
        if req_loc.postal_code and p_postal and req_loc.postal_code.lower() != p_postal:
            return False

        # At least one matching indicator must confirm geographic alignment
        if req_loc.city and p_city and (req_loc.city.lower() in p_city or p_city in req_loc.city.lower()):
            return True
        if req_loc.postal_code and p_postal and req_loc.postal_code.lower() == p_postal:
            return True
        if req_loc.service_area and p_area and (req_loc.service_area.lower() in p_area or p_area in req_loc.service_area.lower()):
            return True

        return False
