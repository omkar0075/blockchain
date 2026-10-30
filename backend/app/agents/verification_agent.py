import copy
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from .base import BaseAgent
from ..schemas.agent_messages import AgentMessage, AgentStatus
from ..schemas.provider_offer import ProviderOffer
from ..schemas.resource_request import FORBIDDEN_PII_KEYWORDS, ResourceRequest
from ..schemas.verification import (
    FailedCheck,
    VerificationChecklist,
    VerificationResult,
    VerificationStatus,
)

logger = logging.getLogger("medway.agents.verification")

# Authoritative Institutional Registry
# Defines accredited institutions, their cryptographic wallets, status, and authorized scopes.
DEFAULT_INSTITUTIONAL_REGISTRY: dict[str, dict[str, Any]] = {
    "hospital-a": {
        "id": "hospital-a",
        "name": "City Hospital A",
        "wallet_address": "0x4dB8336edB0af7FeD1311312081689993F80a18B",
        "is_active": True,
        "authorized_resource_types": ["diagnostic_slot", "icu_bed", "hospital_bed"],
        "service_areas": ["Metropolis"],
    },
    "hospital-b": {
        "id": "hospital-b",
        "name": "Community Hospital B",
        "wallet_address": "0x70997970c51812e33987251372ecdbc2b3f3bb6e",
        "is_active": True,
        "authorized_resource_types": ["diagnostic_slot", "icu_bed", "hospital_bed"],
        "service_areas": ["Metropolis", "Central District"],
    },
    "lab-c": {
        "id": "lab-c",
        "name": "Central Diagnostic Lab C",
        "wallet_address": "0xb05049cc2A5a1c28979DEafc82B82749681DA92C",
        "is_active": True,
        "authorized_resource_types": ["diagnostic_slot"],
        "service_areas": ["Metropolis", "Metropolitan Area"],
    },
    "blood-bank-d": {
        "id": "blood-bank-d",
        "name": "Red Cross Regional Blood Bank",
        "wallet_address": "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
        "is_active": True,
        "authorized_resource_types": ["blood_unit"],
        "service_areas": ["Gotham", "East District"],
    },
    "inactive-clinic-x": {
        "id": "inactive-clinic-x",
        "name": "Inactive Clinic X",
        "wallet_address": "0x90F79bf6EB2c4f870365E785982E1f101E93b906",
        "is_active": False,
        "authorized_resource_types": ["diagnostic_slot"],
        "service_areas": ["Metropolis"],
    },
}

# Authoritative Uncommitted Inventory
# Authoritative source of truth for available, uncommitted capacity (mirrors on-chain state).
DEFAULT_AUTHORITATIVE_INVENTORY: dict[str, dict[str, int]] = {
    "hospital-a": {
        "diagnostic_slot": 0,
        "icu_bed": 1,
        "hospital_bed": 5,
    },
    "hospital-b": {
        "diagnostic_slot": 4,
        "icu_bed": 3,
        "hospital_bed": 10,
    },
    "lab-c": {
        "diagnostic_slot": 8,
    },
    "blood-bank-d": {
        "blood_unit": 25,
    },
    "inactive-clinic-x": {
        "diagnostic_slot": 0,
    },
}


class VerificationAgent(BaseAgent):
    """
    Autonomous Verification Agent.

    Checks whether a candidate provider and its offer are trustworthy, authorized,
    and eligible before negotiation proceeds to institutional commitment.

    Architectural & Governance Invariants:
    1. Independent Authoritative Verification: Verification evaluates authoritative
       institutional registries and capacity sources, NEVER solely claims asserted by
       a provider agent.
    2. Zero Capacity Guarantee: An advertised offer is NOT proof of reservation.
       Capacity remains unreserved until atomically committed via smart contract locking.
    3. Conclusive Determination vs REQUIRES_REVIEW: If authoritative capacity or registry
       data is unreachable or ambiguous, status must be REQUIRES_REVIEW, preventing false
       positive verifications.
    4. Immutable Audit Checklist: Produces a structured VerificationResult recording
       independent checks and failed reason codes.
    5. Zero Transaction Authority: Does not execute blockchain transactions or override
       its own verification failures.
    """

    def __init__(
        self,
        name: str = "VerificationAgent",
        institutional_registry: Optional[dict[str, dict[str, Any]]] = None,
        authoritative_inventory: Optional[dict[str, dict[str, int]]] = None,
        capacity_source: Optional[Callable[..., Any]] = None,
    ) -> None:
        super().__init__(name=name)
        self.institutional_registry = (
            copy.deepcopy(institutional_registry)
            if institutional_registry is not None
            else copy.deepcopy(DEFAULT_INSTITUTIONAL_REGISTRY)
        )
        self.authoritative_inventory = (
            copy.deepcopy(authoritative_inventory)
            if authoritative_inventory is not None
            else copy.deepcopy(DEFAULT_AUTHORITATIVE_INVENTORY)
        )
        self.capacity_source = capacity_source

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Verify provider eligibility and offer integrity.

        Expected payload:
            - resource_request (dict): Validated ResourceRequest.
            - provider_offer (dict): ProviderOffer being verified.
            - operational_wallet (str, optional): Wallet claimed by provider.
            - authoritative_capacity (int | str, optional): Authoritative capacity override ("UNAVAILABLE" or int).
            - authoritative_inventory (dict, optional): Authoritative inventory override.
            - institutional_registry (dict, optional): Registry override.
        """
        payload = message.payload or {}

        # 1. PII Scan
        for k, v in payload.items():
            if str(k).lower() in FORBIDDEN_PII_KEYWORDS:
                raise ValueError(f"Privacy violation: patient identifying field '{k}' detected")

        req_raw = payload.get("resource_request") or payload.get("request")
        offer_raw = payload.get("provider_offer") or payload.get("offer")

        if not req_raw or not offer_raw:
            raise ValueError("VerificationAgent requires 'resource_request' and 'provider_offer' in message payload")

        request = ResourceRequest.model_validate(req_raw)
        offer = ProviderOffer.model_validate(offer_raw)

        # Dynamic registries / overrides
        registry = payload.get("institutional_registry", self.institutional_registry)
        inventory = payload.get("authoritative_inventory", self.authoritative_inventory)

        checks = VerificationChecklist()
        failed_checks: list[FailedCheck] = []
        requires_review = False

        provider_id = offer.provider_id
        now = datetime.now(timezone.utc)

        # Check 1: Provider exists in institutional registry
        if provider_id not in registry:
            checks.institution_registered = False
            failed_checks.append(
                FailedCheck(
                    check_name="institution_registered",
                    reason_code="UNREGISTERED_PROVIDER",
                    message=f"Provider '{provider_id}' is not registered in institutional registry",
                )
            )
        else:
            checks.institution_registered = True

        inst_data = registry.get(provider_id, {})

        # Check 2: Institution is active
        if checks.institution_registered:
            is_active = inst_data.get("is_active", True)
            if not is_active:
                checks.institution_active = False
                failed_checks.append(
                    FailedCheck(
                        check_name="institution_active",
                        reason_code="INACTIVE_INSTITUTION",
                        message=f"Institution '{provider_id}' is inactive or suspended",
                    )
                )
            else:
                checks.institution_active = True
        else:
            checks.institution_active = False

        # Check 3: Institution is authorized for the resource type
        r_type_str = offer.resource_type.value if hasattr(offer.resource_type, "value") else str(offer.resource_type)
        if checks.institution_registered:
            auth_types = inst_data.get("authorized_resource_types", [])
            if auth_types and r_type_str not in auth_types:
                checks.resource_registered = False
                failed_checks.append(
                    FailedCheck(
                        check_name="resource_registered",
                        reason_code="UNAUTHORIZED_RESOURCE_TYPE",
                        message=f"Institution '{provider_id}' is not authorized for resource type '{r_type_str}'",
                    )
                )
            else:
                checks.resource_registered = True
        else:
            checks.resource_registered = False

        # Check 4: Operational wallet matches registered institutional wallet
        registered_wallet = inst_data.get("wallet_address") or inst_data.get("address")
        claimed_wallet = (
            payload.get("operational_wallet")
            or payload.get("wallet_address")
            or (offer.metadata.get("operational_wallet") if offer.metadata else None)
            or (offer.metadata.get("wallet_address") if offer.metadata else None)
        )

        if not checks.institution_registered or not registered_wallet:
            checks.wallet_authorized = False
            failed_checks.append(
                FailedCheck(
                    check_name="wallet_authorized",
                    reason_code="MISSING_REGISTERED_WALLET",
                    message=f"No authorized wallet found for provider '{provider_id}'",
                )
            )
        elif claimed_wallet and claimed_wallet.lower() != registered_wallet.lower():
            checks.wallet_authorized = False
            failed_checks.append(
                FailedCheck(
                    check_name="wallet_authorized",
                    reason_code="WALLET_MISMATCH",
                    message=(
                        f"Operational wallet '{claimed_wallet}' does not match "
                        f"registered institutional wallet '{registered_wallet}'"
                    ),
                )
            )
        else:
            checks.wallet_authorized = True

        # Check 5: Offer matches the original request
        # Compares resource type, quantity bounds, location, and request ID
        req_type_str = request.resource_type.value if hasattr(request.resource_type, "value") else str(request.resource_type)
        if r_type_str != req_type_str:
            checks.request_compatible = False
            failed_checks.append(
                FailedCheck(
                    check_name="request_compatible",
                    reason_code="RESOURCE_TYPE_MISMATCH",
                    message=f"Offered resource type '{r_type_str}' does not match requested '{req_type_str}'",
                )
            )
        elif offer.offered_quantity <= 0:
            checks.request_compatible = False
            failed_checks.append(
                FailedCheck(
                    check_name="request_compatible",
                    reason_code="INVALID_OFFERED_QUANTITY",
                    message=f"Offered quantity must be positive, got {offer.offered_quantity}",
                )
            )
        elif request.location and offer.location and not self._is_location_compatible(request.location, offer.location):
            checks.request_compatible = False
            failed_checks.append(
                FailedCheck(
                    check_name="request_compatible",
                    reason_code="LOCATION_MISMATCH",
                    message=(
                        f"Offer location '{offer.location.city}' is geographically incompatible "
                        f"with requested location '{request.location.city}'"
                    ),
                )
            )
        elif offer.request_id != request.request_id:
            checks.request_compatible = False
            failed_checks.append(
                FailedCheck(
                    check_name="request_compatible",
                    reason_code="REQUEST_ID_MISMATCH",
                    message=f"Offer request_id '{offer.request_id}' does not match request '{request.request_id}'",
                )
            )
        else:
            checks.request_compatible = True

        # Check 6: Offer validity and expiry
        if offer.expires_at <= now:
            checks.offer_unexpired = False
            failed_checks.append(
                FailedCheck(
                    check_name="offer_unexpired",
                    reason_code="OFFER_EXPIRED",
                    message=f"Offer expired at {offer.expires_at.isoformat()}",
                )
            )
        else:
            checks.offer_unexpired = True

        # Check 7: Current capacity using authoritative sources
        auth_cap_raw = payload.get("authoritative_capacity")
        if auth_cap_raw == "UNAVAILABLE" or (auth_cap_raw is None and "authoritative_capacity" in payload):
            checks.capacity_available = False
            failed_checks.append(
                FailedCheck(
                    check_name="capacity_available",
                    reason_code="AUTHORITATIVE_DATA_UNAVAILABLE",
                    message="Authoritative capacity source is currently unavailable or unreachable",
                )
            )
            requires_review = True
        elif isinstance(auth_cap_raw, (int, float)):
            auth_cap = int(auth_cap_raw)
            if auth_cap >= offer.offered_quantity:
                checks.capacity_available = True
            else:
                checks.capacity_available = False
                failed_checks.append(
                    FailedCheck(
                        check_name="capacity_available",
                        reason_code="INSUFFICIENT_AUTHORITATIVE_CAPACITY",
                        message=(
                            f"Authoritative capacity ({auth_cap}) is less than "
                            f"offered quantity ({offer.offered_quantity})"
                        ),
                    )
                )
        else:
            # Query authoritative inventory or callable source
            auth_cap = None
            if self.capacity_source is not None:
                try:
                    res = self.capacity_source(provider_id, r_type_str)
                    if hasattr(res, "__await__"):
                        res = await res
                    auth_cap = res
                except Exception as exc:
                    logger.warning("Error querying capacity source: %s", exc)
                    auth_cap = None

            if auth_cap is None:
                p_inv = inventory.get(provider_id)
                if p_inv and r_type_str in p_inv:
                    raw_val = p_inv[r_type_str]
                    auth_cap = raw_val.get("available", raw_val.get("uncommitted", 0)) if isinstance(raw_val, dict) else raw_val

            if auth_cap is None:
                checks.capacity_available = False
                failed_checks.append(
                    FailedCheck(
                        check_name="capacity_available",
                        reason_code="AUTHORITATIVE_DATA_UNAVAILABLE",
                        message=(
                            f"Authoritative capacity records unavailable for provider '{provider_id}' "
                            f"and resource '{r_type_str}'"
                        ),
                    )
                )
                requires_review = True
            elif auth_cap < offer.offered_quantity:
                checks.capacity_available = False
                failed_checks.append(
                    FailedCheck(
                        check_name="capacity_available",
                        reason_code="INSUFFICIENT_AUTHORITATIVE_CAPACITY",
                        message=(
                            f"Authoritative uncommitted capacity ({auth_cap}) is insufficient "
                            f"for offered quantity ({offer.offered_quantity})"
                        ),
                    )
                )
            else:
                checks.capacity_available = True

        # Status Resolution Logic
        # If any hard, definitive disqualifications exist: REJECTED
        hard_rejections = [
            fc for fc in failed_checks
            if fc.reason_code != "AUTHORITATIVE_DATA_UNAVAILABLE"
        ]

        if hard_rejections:
            status = VerificationStatus.REJECTED
            self.set_status(AgentStatus.COMPLETED)
            reasons = "; ".join(f"{fc.check_name}: {fc.message}" for fc in failed_checks)
            explanation = f"Verification REJECTED for provider '{provider_id}': {reasons}"
            success = False
        elif requires_review:
            status = VerificationStatus.REQUIRES_REVIEW
            self.set_status(AgentStatus.WAITING_FOR_APPROVAL)
            explanation = (
                f"Verification REQUIRES_REVIEW for provider '{provider_id}': "
                "authoritative capacity cannot be conclusively verified. Human/contract review required."
            )
            success = True
        elif checks.is_all_passed() and not failed_checks:
            status = VerificationStatus.VERIFIED
            self.set_status(AgentStatus.COMPLETED)
            explanation = (
                f"Provider '{provider_id}' and offer successfully VERIFIED. "
                "Point-in-time snapshot valid. Capacity remains unreserved pending smart contract commitment."
            )
            success = True
        else:
            status = VerificationStatus.REJECTED
            self.set_status(AgentStatus.COMPLETED)
            explanation = f"Verification checks incomplete for provider '{provider_id}'"
            success = False

        # Invariant 2: Advertised offer is NEVER proof of reserved capacity
        verification = VerificationResult(
            workflow_id=message.workflow_id,
            provider_id=provider_id,
            offer_id=offer.offer_id,
            request_id=request.request_id,
            status=status,
            checks=checks,
            failed_checks=failed_checks,
            verified_at=now,
            explanation=explanation,
            metadata={
                "capacity_reserved": False,
                "is_advertised_only": True,
                "requires_onchain_lock": True,
            },
        )

        return {
            "success": success,
            "status": status.value,
            "capacity_reserved": False,
            "verification": verification.model_dump(),
            "checks": checks.model_dump(),
            "failed_checks": [fc.model_dump() for fc in failed_checks],
            "explanation": explanation,
            "message": explanation,
        }

    def _is_location_compatible(self, req_loc: Any, offer_loc: Any) -> bool:
        """Evaluate geographic compatibility between request and offer."""
        r_city = getattr(req_loc, "city", None)
        o_city = getattr(offer_loc, "city", None)
        r_postal = getattr(req_loc, "postal_code", None)
        o_postal = getattr(offer_loc, "postal_code", None)

        if r_city and o_city and r_city.lower() not in o_city.lower() and o_city.lower() not in r_city.lower():
            return False

        if r_postal and o_postal and r_postal.lower() != o_postal.lower():
            return False

        return True

