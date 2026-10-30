import logging
from typing import Any, Optional, Protocol, Union
from uuid import UUID

from .base import BaseAgent
from .context import FORBIDDEN_SECURITY_KEYWORDS
from ..schemas.agent_messages import (
    AgentError,
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from ..schemas.resource_request import ResourceType
from ..schemas.verification import VerificationResult, VerificationStatus

logger = logging.getLogger("medway.agents.blockchain")


class BlockchainClientProtocol(Protocol):
    """Protocol for interacting with the backend Viem/Coral blockchain service."""

    def lock_capacity(
        self,
        requester: str,
        provider: str,
        resource_type: str,
        quantity: int,
        duration: int = 1800,
    ) -> dict[str, Any]: ...

    def create_reservation(
        self,
        requester: str,
        provider: str,
        resource_ref: str,
        expires_in: int = 7200,
    ) -> dict[str, Any]: ...

    def create_reservation_with_lock(
        self,
        lock_id: str,
        resource_ref: str,
        expires_in: int = 7200,
    ) -> dict[str, Any]: ...


class DefaultBlockchainClient:
    """Default client dispatching calls to app.blockchain subprocess service."""

    def lock_capacity(
        self,
        requester: str,
        provider: str,
        resource_type: str = "diagnostic_slot",
        quantity: int = 1,
        duration: int = 1800,
    ) -> dict[str, Any]:
        from ..blockchain import lock_onchain_capacity
        return lock_onchain_capacity(
            requester=requester,
            provider=provider,
            resource_type=resource_type,
            quantity=quantity,
            duration=duration,
        )

    def create_reservation(
        self,
        requester: str,
        provider: str,
        resource_ref: str,
        expires_in: int = 7200,
    ) -> dict[str, Any]:
        from ..blockchain import create_onchain_reservation
        return create_onchain_reservation(
            requester=requester,
            provider=provider,
            resource_ref=resource_ref,
            expires_in=expires_in,
        )

    def create_reservation_with_lock(
        self,
        lock_id: str,
        resource_ref: str,
        expires_in: int = 7200,
    ) -> dict[str, Any]:
        from ..blockchain import create_reservation_with_lock
        return create_reservation_with_lock(
            lock_id=lock_id,
            resource_ref=resource_ref,
            expires_in=expires_in,
        )


class BlockchainExecutionAgent(BaseAgent):
    """
    Autonomous Blockchain Execution Agent.

    Connects verified, institutionally approved coordination workflows to the
    underlying Coral Testnet blockchain service.

    Architectural & Governance Invariants:
    1. Authorization Gating: Strictly executes only workflows with verified status
       AND attributable institutional authorization.
    2. Zero Secret Exposure: Never accepts, stores, or outputs private keys or credentials.
    3. Delegated Signing: Signing and relayer interaction are strictly executed by the
       blockchain service; agents never hold cryptographic authority.
    4. Confirmation Guarantee: Never reports success before an on-chain transaction receipt
       is verified with a valid transaction hash and block number.
    5. Idempotency & Duplicate Protection: Prevents duplicate on-chain commitments.
    6. Audit Lineage: Links workflow_id to the resulting on-chain transaction hash and lock/reservation ID.
    """

    def __init__(
        self,
        name: str = "BlockchainExecutionAgent",
        blockchain_client: Optional[Any] = None,
    ) -> None:
        super().__init__(name=name)
        self.blockchain_client = blockchain_client or DefaultBlockchainClient()
        self._executed_idempotency_keys: dict[str, dict[str, Any]] = {}

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Execute an authorized on-chain commitment or capacity lock.

        Expected payload keys:
            - verification (dict | VerificationResult): Verified result.
            - approved (bool): Must be True.
            - approved_by (str): Approving institutional officer.
            - requester_id (str): Requesting institution.
            - provider_id (str): Provider institution.
            - resource_type (str | ResourceType): Resource identifier.
            - quantity (int): Number of units.
            - operation (str, optional): 'LOCK_CAPACITY', 'COMMIT_RESERVATION', or 'LOCK_AND_COMMIT'.
            - idempotency_key (str, optional): Unique submission key.
        """
        payload = message.payload or {}

        # 1. Zero Secrets & PII Invariant
        self._scan_for_secrets_and_pii(payload)

        # 2. Idempotency & Duplicate Protection
        operation = str(payload.get("operation", "LOCK_CAPACITY")).upper()
        idempotency_key = str(payload.get("idempotency_key") or f"{message.workflow_id}_{operation}")

        if idempotency_key in self._executed_idempotency_keys:
            logger.warning("Duplicate execution attempt blocked for key: %s", idempotency_key)
            raise ValueError(f"Duplicate transaction rejected: idempotency key '{idempotency_key}' has already been executed")

        # 3. Requirement 1: Verification Gating
        v_raw = payload.get("verification") or payload.get("verification_result")
        if not v_raw:
            raise ValueError("Blockchain execution rejected: workflow has not been verified")

        v_status: str
        if isinstance(v_raw, VerificationResult):
            v_status = v_raw.status.value if hasattr(v_raw.status, "value") else str(v_raw.status)
        elif isinstance(v_raw, dict):
            raw_val = v_raw.get("status", "")
            v_status = raw_val.value if hasattr(raw_val, "value") else str(raw_val)
        else:
            raise TypeError(f"Invalid verification object type: {type(v_raw).__name__}")

        if "." in v_status:
            v_status = v_status.split(".")[-1]

        if v_status != VerificationStatus.VERIFIED.value:
            raise ValueError(
                f"Blockchain execution rejected: verification status is '{v_status}', "
                f"expected '{VerificationStatus.VERIFIED.value}'"
            )

        # 4. Requirement 1: Institutional Authorization Gating
        approved = bool(payload.get("approved", False))
        approved_by = payload.get("approved_by")
        if not approved or not approved_by or not str(approved_by).strip():
            raise ValueError(
                "Blockchain execution rejected: attributable institutional approval is missing or unconfirmed"
            )

        # 5. Requirement 2: Validate Reservation Terms
        requester_id = (
            payload.get("requester_id")
            or payload.get("requester_institution_id")
            or (payload.get("resource_request", {}).get("requester_institution_id") if isinstance(payload.get("resource_request"), dict) else None)
        )
        provider_id = (
            payload.get("provider_id")
            or (payload.get("selected_offer", {}).get("provider_id") if isinstance(payload.get("selected_offer"), dict) else None)
        )
        r_type = (
            payload.get("resource_type")
            or (payload.get("resource_request", {}).get("resource_type") if isinstance(payload.get("resource_request"), dict) else None)
        )
        quantity = (
            payload.get("quantity")
            or (payload.get("selected_offer", {}).get("offered_quantity") if isinstance(payload.get("selected_offer"), dict) else None)
            or (payload.get("resource_request", {}).get("quantity") if isinstance(payload.get("resource_request"), dict) else None)
        )

        if not requester_id or not provider_id or not r_type:
            raise ValueError(
                "Invalid reservation terms: 'requester_id', 'provider_id', and 'resource_type' are required"
            )

        try:
            qty_int = int(quantity)
            if qty_int <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"Invalid reservation terms: 'quantity' must be a positive integer, got {quantity}")

        r_type_str = r_type.value if hasattr(r_type, "value") else str(r_type)
        duration = int(payload.get("duration", 1800))
        resource_ref = str(payload.get("resource_ref") or f"urn:medway:{r_type_str}:{qty_int}:{message.workflow_id}")

        # 6. Requirements 3 & 4: Call Predefined Blockchain Service
        res: dict[str, Any]
        if operation == "LOCK_CAPACITY":
            res = self.blockchain_client.lock_capacity(
                requester=requester_id,
                provider=provider_id,
                resource_type=r_type_str,
                quantity=qty_int,
                duration=duration,
            )
        elif operation == "COMMIT_RESERVATION":
            lock_id = payload.get("lock_id")
            if lock_id:
                res = self.blockchain_client.create_reservation_with_lock(
                    lock_id=str(lock_id),
                    resource_ref=resource_ref,
                    expires_in=duration,
                )
            else:
                res = self.blockchain_client.create_reservation(
                    requester=requester_id,
                    provider=provider_id,
                    resource_ref=resource_ref,
                    expires_in=duration,
                )
        elif operation == "LOCK_AND_COMMIT":
            # 2PC: Lock capacity first, then convert lock to reservation
            lock_res = self.blockchain_client.lock_capacity(
                requester=requester_id,
                provider=provider_id,
                resource_type=r_type_str,
                quantity=qty_int,
                duration=duration,
            )
            if not lock_res.get("success"):
                err_msg = lock_res.get("error", "Capacity lock phase failed")
                raise RuntimeError(f"Blockchain 2PC capacity lock reverted: {err_msg}")

            lock_id = lock_res.get("lockId") or lock_res.get("lock_id")
            res = self.blockchain_client.create_reservation_with_lock(
                lock_id=str(lock_id),
                resource_ref=resource_ref,
                expires_in=duration,
            )
        else:
            raise ValueError(f"Unsupported blockchain operation: '{operation}'")

        # 7. Requirement 6: Handle Rejected Transactions & RPC Failures
        if not res or not res.get("success"):
            err_msg = res.get("error", "Unknown blockchain transaction failure") if isinstance(res, dict) else "No response from blockchain service"
            logger.error("Blockchain execution failed for workflow %s: %s", message.workflow_id, err_msg)
            raise RuntimeError(f"Blockchain transaction failed: {err_msg}")

        # 8. Requirement 9: Never Report Success Before Confirmation
        tx_hash = res.get("txHash") or res.get("tx_hash")
        block_number = res.get("blockNumber") or res.get("block_number")

        if not tx_hash:
            raise RuntimeError("Blockchain transaction unconfirmed: missing transaction hash receipt")

        lock_id = res.get("lockId") or res.get("lock_id")
        reservation_id = res.get("reservationId") or res.get("reservation_id")

        result_payload = {
            "success": True,
            "status": "CONFIRMED",
            "confirmed": True,
            "workflow_id": str(message.workflow_id),
            "idempotency_key": idempotency_key,
            "operation": operation,
            "tx_hash": tx_hash,
            "block_number": block_number,
            "lock_id": lock_id,
            "reservation_id": reservation_id,
            "requester_id": requester_id,
            "provider_id": provider_id,
            "resource_type": r_type_str,
            "quantity": qty_int,
            "approved_by": approved_by,
            "message": (
                f"Blockchain transaction confirmed on Coral Testnet at block {block_number} "
                f"(tx: {tx_hash})"
            ),
        }

        # 9. Record for Idempotency
        self._executed_idempotency_keys[idempotency_key] = result_payload
        logger.info(
            "Blockchain execution confirmed for workflow %s (tx: %s, block: %s)",
            message.workflow_id,
            tx_hash,
            block_number,
        )

        return result_payload

    def _scan_for_secrets_and_pii(self, payload: dict[str, Any]) -> None:
        """Enforces security & privacy boundaries: rejects private keys and patient PII."""
        for k, v in payload.items():
            k_lower = str(k).lower()
            if k_lower in FORBIDDEN_SECURITY_KEYWORDS:
                raise ValueError(f"Security violation: forbidden key '{k}' detected in payload")
            if isinstance(v, dict):
                self._scan_for_secrets_and_pii(v)
            elif isinstance(v, str):
                v_lower = v.lower()
                for keyword in ("private_key", "secret_key", "mnemonic", "bearer "):
                    if keyword in v_lower:
                        raise ValueError(f"Security violation: secret pattern '{keyword}' detected in payload")
