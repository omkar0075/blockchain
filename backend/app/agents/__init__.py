import logging
from .base import BaseAgent
from .context import AgentContext
from .discovery_agent import DiscoveryAgent
from .message_bus import MessageBus, default_bus
from .negotiation_agent import NegotiationAgent
from .provider_agent import ProviderAgent
from .registry import (
    AgentRegistry,
    clear_registry,
    default_registry,
    get_agent,
    list_agents,
    register_agent,
)
from .request_agent import (
    BaseRequestParser,
    DeterministicRequestParser,
    RequestAgent,
)
from .verification_agent import VerificationAgent
from .blockchain_agent import BlockchainExecutionAgent, DefaultBlockchainClient
from .orchestrator import (
    WorkflowOrchestrator,
    WorkflowRecord,
    WorkflowState,
    WorkflowTransition,
    create_default_registry,
)
from ..blockchain import get_onchain_capacity
from ..data import INSTITUTIONS
from ..models import ResourceRequest, AllocationOffer

logger = logging.getLogger("medway.agents")

URGENCY_ORDER = {"critical": 0, "urgent": 1, "routine": 2}


def discover_and_verify(request: ResourceRequest) -> list[AllocationOffer]:
    """
    AI discovery and negotiation agent.
    
    Architectural Invariant:
    The AI agent is NEVER the authority on resource availability.
    The agent discovers registered candidate providers and inspects real-time
    available capacity strictly enforced by on-chain smart contracts on Coral Testnet.
    """
    offers: list[AllocationOffer] = []
    
    for institution in INSTITUTIONS:
        if institution["id"] == request.requesting_institution_id:
            continue

        provider_id = institution["id"]
        provider_name = institution["name"]
        resource_type = request.resource_type

        # 1. Query on-chain capacity directly from Coral Testnet
        onchain_verified = False
        available_qty = 0
        total_capacity = 0
        is_active = True

        try:
            cap_res = get_onchain_capacity(provider_id, resource_type)
            if cap_res.get("success"):
                is_active = bool(cap_res.get("isActive", True))
                if not is_active:
                    logger.info("Provider %s is inactive on-chain for resource %s", provider_id, resource_type)
                    continue
                available_qty = int(cap_res.get("availableCapacity", 0))
                total_capacity = int(cap_res.get("totalCapacity", 0))
                onchain_verified = True
        except Exception as e:
            logger.warning("Failed to query on-chain capacity for %s: %s", provider_id, e)

        # 2. Fallback to catalog data only if on-chain query was unavailable or returned 0 total capacity
        if not onchain_verified or total_capacity == 0:
            catalog_qty = int(institution["resources"].get(resource_type, 0))
            if catalog_qty <= 0:
                continue
            available_qty = catalog_qty
            rationale = "Synthetic catalog capacity; offline fallback pending on-chain quota."
        else:
            if available_qty <= 0:
                # On-chain capacity is depleted (all slots locked or committed)
                continue
            rationale = f"Real-time available capacity ({available_qty}/{total_capacity} slots) strictly verified on Coral Testnet."

        delivery_hours = 2 if institution["type"] == "diagnostic_lab" else 6
        if request.urgency == "critical":
            delivery_hours = max(1, delivery_hours // 2)

        offers.append(
            AllocationOffer(
                provider_id=provider_id,
                provider_name=provider_name,
                resource_type=resource_type,
                available_quantity=available_qty,
                estimated_delivery_hours=delivery_hours,
                rationale=rationale,
                onchain_verified=onchain_verified,
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


__all__ = [
    "AgentContext",
    "AgentRegistry",
    "BaseAgent",
    "clear_registry",
    "default_bus",
    "default_registry",
    "discover_and_verify",
    "choose_offer",
    "DiscoveryAgent",
    "get_agent",
    "list_agents",
    "MessageBus",
    "NegotiationAgent",
    "ProviderAgent",
    "register_agent",
    "RequestAgent",
    "VerificationAgent",
    "BlockchainExecutionAgent",
    "WorkflowOrchestrator",
    "WorkflowRecord",
    "WorkflowState",
    "WorkflowTransition",
    "create_default_registry",
    "BaseRequestParser",
    "DeterministicRequestParser",
]
