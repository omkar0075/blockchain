from .data import INSTITUTIONS
from .models import ResourceRequest, AllocationOffer

URGENCY_ORDER = {"critical": 0, "urgent": 1, "routine": 2}

def discover_and_verify(request: ResourceRequest) -> list[AllocationOffer]:
    """Demo provider agents report synthetic capacity; freshness is assumed for MVP."""
    offers = []
    for institution in INSTITUTIONS:
        if institution["id"] == request.requesting_institution_id:
            continue
        quantity = int(institution["resources"].get(request.resource_type, 0))
        if quantity <= 0:
            continue
        offers.append(
            AllocationOffer(
                provider_id=institution["id"],
                provider_name=institution["name"],
                resource_type=request.resource_type,
                available_quantity=quantity,
                estimated_delivery_hours=2 if institution["type"] == "diagnostic_lab" else 6,
                rationale="Synthetic demo capacity; must be verified against provider operational records in production.",
            )
        )
    return offers

def choose_offer(request: ResourceRequest, offers: list[AllocationOffer]) -> AllocationOffer | None:
    feasible = [
        offer for offer in offers
        if offer.available_quantity >= request.quantity
        and offer.estimated_delivery_hours <= request.required_by_hours
    ]
    if not feasible:
        return None
    # Simple deterministic baseline: shortest time, then smallest excess capacity.
    return sorted(
        feasible,
        key=lambda offer: (offer.estimated_delivery_hours,
                           offer.available_quantity - request.quantity)
    )[0]
