import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from .base import BaseAgent
from ..schemas.agent_messages import AgentMessage, AgentStatus
from ..schemas.provider_offer import OfferStatus, ProviderCost, ProviderOffer
from ..schemas.resource_request import LocationArea, ResourceRequest, ResourceType

logger = logging.getLogger("medway.agents.discovery")

DEFAULT_PROVIDER_CATALOG: list[dict[str, Any]] = [
    {
        "id": "hospital-b",
        "name": "Community Hospital B",
        "type": "hospital",
        "city": "Metropolis",
        "postal_code": "560001",
        "service_areas": ["Metropolis", "Central District"],
        "is_active": True,
        "resources": {
            "diagnostic_slot": {"advertised": 4, "available": 4},
            "icu_bed": {"advertised": 6, "available": 3},
            "hospital_bed": {"advertised": 20, "available": 10},
        },
        "delivery_hours": 4,
    },
    {
        "id": "lab-c",
        "name": "Central Diagnostic Lab C",
        "type": "diagnostic_lab",
        "city": "Metropolis",
        "postal_code": "560001",
        "service_areas": ["Metropolis", "Metropolitan Area"],
        "is_active": True,
        "resources": {
            "diagnostic_slot": {"advertised": 12, "available": 8},
        },
        "delivery_hours": 2,
    },
    {
        "id": "blood-bank-d",
        "name": "Red Cross Regional Blood Bank",
        "type": "blood_bank",
        "city": "Gotham",
        "postal_code": "560002",
        "service_areas": ["Gotham", "East District"],
        "is_active": True,
        "resources": {
            "blood_unit": {"advertised": 50, "available": 25},
        },
        "delivery_hours": 3,
    },
    {
        "id": "ambulance-service-e",
        "name": "Metro Emergency Transport",
        "type": "ambulance_service",
        "city": "Metropolis",
        "postal_code": "560001",
        "service_areas": ["Metropolis"],
        "is_active": False,  # Inactive provider for testing exclusion
        "resources": {
            "ambulance": {"advertised": 5, "available": 2},
        },
        "delivery_hours": 1,
    },
]


class DiscoveryAgent(BaseAgent):
    """
    Autonomous Resource Discovery Agent.
    
    Discovers candidate healthcare resource providers matching a validated ResourceRequest.
    
    Architectural Invariant:
    Discovery identifies and ranks candidates based on catalog/cached inventory.
    It NEVER claims authority over live availability, reserves capacity, or executes
    blockchain transactions. Smart contract locking remains the sole commitment authority.
    """

    def __init__(
        self,
        name: str = "DiscoveryAgent",
        catalog: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        super().__init__(name=name)
        self.catalog = catalog if catalog is not None else DEFAULT_PROVIDER_CATALOG

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Process a resource discovery task for a validated ResourceRequest.
        
        Expected payload:
            - resource_request (dict) or request (dict)
        """
        payload = message.payload or {}
        req_raw = payload.get("resource_request") or payload.get("request") or payload

        try:
            request = ResourceRequest.model_validate(req_raw)
        except Exception as exc:
            raise ValueError(f"DiscoveryAgent requires a valid ResourceRequest: {exc}")

        # 1. Discover and Filter Candidates
        candidates: list[dict[str, Any]] = []
        r_type_str = request.resource_type.value if hasattr(request.resource_type, "value") else str(request.resource_type)

        preferred_set = set(request.constraints.preferred_providers) if request.constraints else set()
        excluded_set = set(request.constraints.excluded_providers) if request.constraints else set()

        for provider in self.catalog:
            p_id = provider.get("id", "")
            p_name = provider.get("name", "Unknown Provider")
            p_type = provider.get("type", "healthcare_facility")

            # Invariant: Provider cannot fulfill its own request
            if p_id == request.requester_institution_id:
                continue

            # Invariant: Exclude inactive providers
            if not provider.get("is_active", True):
                logger.info("Excluding inactive provider '%s'", p_id)
                continue

            # Invariant: Exclude explicitly barred providers
            if p_id in excluded_set:
                logger.info("Excluding explicitly barred provider '%s'", p_id)
                continue

            # Invariant: Filter by resource type
            resources = provider.get("resources", {})
            if r_type_str not in resources:
                continue

            res_info = resources[r_type_str]
            if isinstance(res_info, dict):
                advertised = int(res_info.get("advertised", 0))
                available = int(res_info.get("available", advertised))
            else:
                advertised = int(res_info)
                available = int(res_info)

            # Invariant: Filter by sufficient available capacity
            if available < request.quantity:
                logger.info(
                    "Excluding provider '%s': insufficient capacity (%d < %d)",
                    p_id,
                    available,
                    request.quantity,
                )
                continue

            # Invariant: Filter by location/service area compatibility
            if request.location:
                if not self._is_location_compatible(request.location, provider):
                    logger.info("Excluding provider '%s': location/service area mismatch", p_id)
                    continue

            # Base delivery SLA calculation
            delivery_hours = int(provider.get("delivery_hours", 4))
            if request.urgency == "critical":
                delivery_hours = max(1, delivery_hours // 2)

            # Build candidate metadata
            is_preferred = p_id in preferred_set
            excess_capacity = available - request.quantity

            candidates.append({
                "provider": provider,
                "advertised_capacity": advertised,
                "available_capacity": available,
                "estimated_delivery_hours": delivery_hours,
                "is_preferred": is_preferred,
                "excess_capacity": excess_capacity,
            })

        # 2. Return No-Match Clarification if no suitable provider found
        if not candidates:
            return {
                "success": True,
                "matches_found": 0,
                "candidate_offers": [],
                "message": (
                    f"No eligible providers found with available capacity for {request.quantity} "
                    f"units of {r_type_str} in target area."
                ),
            }

        # 3. Rank Candidates
        # Ranking hierarchy:
        # 1. Preferred provider status (is_preferred: True first)
        # 2. Shortest estimated delivery hours
        # 3. Smallest excess capacity (best inventory allocation fit)
        ranked = sorted(
            candidates,
            key=lambda c: (
                0 if c["is_preferred"] else 1,
                c["estimated_delivery_hours"],
                c["excess_capacity"],
            ),
        )

        # 4. Construct Output ProviderOffer Candidates
        now = datetime.now(timezone.utc)
        offers: list[dict[str, Any]] = []

        for item in ranked:
            p = item["provider"]
            offer = ProviderOffer(
                request_id=request.request_id,
                workflow_id=message.workflow_id,
                provider_id=p["id"],
                provider_name=p["name"],
                institution_type=p.get("type"),
                resource_type=request.resource_type,
                offered_quantity=request.quantity,
                location=LocationArea(
                    city=p.get("city"),
                    postal_code=p.get("postal_code"),
                    service_area=p.get("service_areas", [None])[0] if p.get("service_areas") else None,
                ),
                available_from=now,
                available_until=now + timedelta(days=2),
                expires_at=now + timedelta(hours=min(request.required_by_hours or 24, 24)),
                estimated_delivery_hours=item["estimated_delivery_hours"],
                onchain_verified=False,  # Contract remains the final commitment authority
                status=OfferStatus.OFFERED,
                rationale=(
                    f"Discovered {item['available_capacity']} available uncommitted slots "
                    f"(advertised total: {item['advertised_capacity']}) with {item['estimated_delivery_hours']}h delivery SLA."
                ),
                metadata={
                    "advertised_capacity": item["advertised_capacity"],
                    "available_capacity": item["available_capacity"],
                    "excess_capacity": item["excess_capacity"],
                    "is_preferred": item["is_preferred"],
                },
            )
            offers.append(offer.model_dump())

        return {
            "success": True,
            "matches_found": len(offers),
            "candidate_offers": offers,
            "message": f"Successfully discovered and ranked {len(offers)} eligible provider candidates.",
        }

    def _is_location_compatible(self, req_loc: LocationArea, provider: dict[str, Any]) -> bool:
        """Evaluate geographic compatibility between request location and provider service area."""
        p_city = (provider.get("city") or "").lower()
        p_postal = (provider.get("postal_code") or "").lower()
        p_areas = [str(a).lower() for a in provider.get("service_areas", [])]

        # If both specify cities, they must be compatible
        if req_loc.city and p_city and req_loc.city.lower() not in p_city and p_city not in req_loc.city.lower():
            return False

        # If both specify postal codes, they must match
        if req_loc.postal_code and p_postal and req_loc.postal_code.lower() != p_postal:
            return False

        if req_loc.city and p_city and (req_loc.city.lower() in p_city or p_city in req_loc.city.lower()):
            return True
        if req_loc.postal_code and p_postal and req_loc.postal_code.lower() == p_postal:
            return True
        if req_loc.service_area and any(req_loc.service_area.lower() in a or a in req_loc.service_area.lower() for a in p_areas):
            return True

        return False

