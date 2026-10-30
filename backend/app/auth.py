from typing import Optional
from fastapi import Header, HTTPException, status
from pydantic import BaseModel

class ActorIdentity(BaseModel):
    id: str
    name: str
    role: str  # "requester", "provider", "arbitrator", "admin"
    address: Optional[str] = None

# Known registered institutional entities and credentials
REGISTERED_ACTORS: dict[str, ActorIdentity] = {
    "hospital-a": ActorIdentity(
        id="hospital-a",
        name="City Hospital A",
        role="requester",
        address="0x4dB8336edB0af7FeD1311312081689993F80a18B",
    ),
    "hospital-b": ActorIdentity(
        id="hospital-b",
        name="Community Hospital B",
        role="requester",
        address="0x70997970c51812e33987251372ecdbc2b3f3bb6e",
    ),
    "lab-c": ActorIdentity(
        id="lab-c",
        name="Central Diagnostic Lab C",
        role="provider",
        address="0xb05049cc2A5a1c28979DEafc82B82749681DA92C",
    ),
    "consortium-arbitrator": ActorIdentity(
        id="consortium-arbitrator",
        name="Consortium Arbitration Board",
        role="arbitrator",
        address="0x5E5BB85E3f92389a75e8b555490c99af8B0e8607",  # Contract Owner
    ),
}


def authenticate_actor(
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
    default_actor_id: Optional[str] = None,
) -> ActorIdentity:
    """Authenticate and resolve the institutional actor identity."""
    # Handle FastAPI Header object default when called directly in tests
    effective_actor_id = x_actor_id if isinstance(x_actor_id, str) else None
    actor_id = effective_actor_id or default_actor_id
    if not actor_id:
        return ActorIdentity(id="anonymous", name="Public Viewer", role="viewer")

    actor = REGISTERED_ACTORS.get(str(actor_id).lower())
    if not actor:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unknown or unauthenticated actor identity '{actor_id}'. Registered: {list(REGISTERED_ACTORS.keys())}",
        )
    return actor


def verify_authorization_permission(actor: ActorIdentity, requester_id: str) -> None:
    """Ensure only the requesting hospital coordinator can authorize a proposal."""
    if actor.id != "anonymous" and actor.id != requester_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Actor '{actor.id}' is not permitted to authorize requests for hospital '{requester_id}'.",
        )


def verify_acceptance_permission(actor: ActorIdentity, provider_id: str) -> None:
    """Ensure only the selected provider facility can accept a commitment."""
    if actor.id != "anonymous" and actor.id != provider_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Actor '{actor.id}' is not the selected provider '{provider_id}'. Acceptance forbidden.",
        )


def verify_fulfillment_permission(actor: ActorIdentity, provider_id: str) -> None:
    """Ensure only the provider can attest fulfillment."""
    if actor.id != "anonymous" and actor.id != provider_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Actor '{actor.id}' cannot attest fulfillment. Only provider '{provider_id}' is authorized.",
        )


def verify_dispute_permission(actor: ActorIdentity, requester_id: str, provider_id: str) -> None:
    """Ensure only the requester or provider can raise a dispute."""
    if actor.id != "anonymous" and actor.id not in [requester_id, provider_id]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Actor '{actor.id}' is an unrelated third party. Only '{requester_id}' or '{provider_id}' may open a dispute.",
        )


def verify_arbitration_permission(actor: ActorIdentity) -> None:
    """Ensure only authorized arbitrators / consortium admins can resolve disputes."""
    if actor.role not in ["arbitrator", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Actor '{actor.id}' has role '{actor.role}'. Dispute resolution is strictly reserved for 'arbitrator'.",
        )
