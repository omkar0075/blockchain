import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.agents.blockchain_agent import BlockchainExecutionAgent
from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)
from app.schemas.verification import (
    VerificationChecklist,
    VerificationResult,
    VerificationStatus,
)


class MockBlockchainClient:
    """Mock client simulating smart contract and RPC responses."""

    def __init__(
        self,
        lock_response: Optional[dict[str, Any]] = None,
        reservation_response: Optional[dict[str, Any]] = None,
    ) -> None:
        self.lock_response = lock_response or {
            "success": True,
            "txHash": "0x4a9b6c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b",
            "blockNumber": 1284560,
            "lockId": "0x1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff",
        }
        self.reservation_response = reservation_response or {
            "success": True,
            "txHash": "0x9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e",
            "blockNumber": 1284565,
            "reservationId": "0x2222333344445555666677778888999900001111bbbbccccddddeeeeffffaaaa",
        }
        self.calls: list[dict[str, Any]] = []

    def lock_capacity(
        self,
        requester: str,
        provider: str,
        resource_type: str,
        quantity: int,
        duration: int = 1800,
    ) -> dict[str, Any]:
        self.calls.append({
            "action": "lock_capacity",
            "requester": requester,
            "provider": provider,
            "resource_type": resource_type,
            "quantity": quantity,
            "duration": duration,
        })
        return self.lock_response

    def create_reservation_with_lock(
        self,
        lock_id: str,
        resource_ref: str,
        expires_in: int = 7200,
    ) -> dict[str, Any]:
        self.calls.append({
            "action": "create_reservation_with_lock",
            "lock_id": lock_id,
            "resource_ref": resource_ref,
            "expires_in": expires_in,
        })
        return self.reservation_response

    def create_reservation(
        self,
        requester: str,
        provider: str,
        resource_ref: str,
        expires_in: int = 7200,
    ) -> dict[str, Any]:
        self.calls.append({
            "action": "create_reservation",
            "requester": requester,
            "provider": provider,
            "resource_ref": resource_ref,
            "expires_in": expires_in,
        })
        return self.reservation_response


def make_valid_payload(
    workflow_id: Optional[UUID] = None,
    approved: bool = True,
    approved_by: Optional[str] = "Dr. Alice Smith (CMO)",
    verification_status: VerificationStatus = VerificationStatus.VERIFIED,
    quantity: int = 2,
    operation: str = "LOCK_CAPACITY",
) -> dict[str, Any]:
    wf_id = workflow_id or uuid4()
    v_result = VerificationResult(
        workflow_id=wf_id,
        provider_id="lab-c",
        offer_id=uuid4(),
        status=verification_status,
        checks=VerificationChecklist(
            institution_registered=True,
            institution_active=True,
            wallet_authorized=True,
            resource_registered=True,
            offer_unexpired=True,
            capacity_available=True,
            request_compatible=True,
        ) if verification_status == VerificationStatus.VERIFIED else VerificationChecklist(),
        verified_at=datetime.now(timezone.utc),
    )

    return {
        "workflow_id": str(wf_id),
        "verification": v_result.model_dump(),
        "approved": approved,
        "approved_by": approved_by,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "requester_id": "hospital-a",
        "provider_id": "lab-c",
        "resource_type": "diagnostic_slot",
        "quantity": quantity,
        "operation": operation,
    }


def test_rejects_unverified_workflows():
    """Verify that unverified or rejected workflows are refused execution."""
    async def run():
        agent = BlockchainExecutionAgent(blockchain_client=MockBlockchainClient())
        wf_id = uuid4()
        # Verification status REJECTED
        payload = make_valid_payload(
            workflow_id=wf_id,
            verification_status=VerificationStatus.REJECTED,
        )
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "verification status is 'REJECTED'" in result.error.message

    asyncio.run(run())


def test_rejects_workflows_without_approval():
    """Verify that workflows without explicit institutional approval cannot be committed."""
    async def run():
        agent = BlockchainExecutionAgent(blockchain_client=MockBlockchainClient())
        wf_id = uuid4()
        # Approved is False and approved_by is None
        payload = make_valid_payload(
            workflow_id=wf_id,
            approved=False,
            approved_by=None,
        )
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "institutional approval is missing" in result.error.message.lower()

    asyncio.run(run())


def test_executes_authorized_capacity_lock():
    """Verify successful capacity lock on Coral Testnet with receipt confirmation."""
    async def run():
        mock_client = MockBlockchainClient()
        agent = BlockchainExecutionAgent(blockchain_client=mock_client)
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id, operation="LOCK_CAPACITY")
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is True
        assert result.data["status"] == "CONFIRMED"
        assert result.data["confirmed"] is True
        assert result.data["tx_hash"].startswith("0x")
        assert result.data["block_number"] == 1284560
        assert result.data["lock_id"] is not None

        # Verify mock client call
        assert len(mock_client.calls) == 1
        assert mock_client.calls[0]["action"] == "lock_capacity"
        assert mock_client.calls[0]["requester"] == "hospital-a"
        assert mock_client.calls[0]["quantity"] == 2

    asyncio.run(run())


def test_handles_successful_transaction_receipts():
    """Verify handling of two-phase commit reservation transactions."""
    async def run():
        mock_client = MockBlockchainClient()
        agent = BlockchainExecutionAgent(blockchain_client=mock_client)
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id, operation="COMMIT_RESERVATION")
        payload["lock_id"] = "0xlock999"
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.COMPLETED
        assert result.data["operation"] == "COMMIT_RESERVATION"
        assert result.data["reservation_id"] is not None
        assert result.data["tx_hash"].startswith("0x")
        assert result.data["block_number"] == 1284565

    asyncio.run(run())


def test_handles_reverted_transactions():
    """Verify that an on-chain reverted transaction fails cleanly with error capture."""
    async def run():
        reverting_client = MockBlockchainClient(
            lock_response={
                "success": False,
                "error": "Execution reverted: Insufficient uncommitted capacity for provider lab-c",
            }
        )
        agent = BlockchainExecutionAgent(blockchain_client=reverting_client)
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id)
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "insufficient uncommitted capacity" in result.error.message.lower()

    asyncio.run(run())


def test_handles_rpc_failures_and_timeouts():
    """Verify that RPC timeouts or transport errors are captured gracefully without crashing."""
    async def run():
        timeout_client = MockBlockchainClient(
            lock_response={
                "success": False,
                "error": "Operation timed out after 45s while waiting for transaction receipt",
            }
        )
        agent = BlockchainExecutionAgent(blockchain_client=timeout_client)
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id)
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "timed out" in result.error.message.lower()

    asyncio.run(run())


def test_prevents_duplicate_execution():
    """Idempotency Invariant: Prevent duplicate execution of transactions for the same idempotency key."""
    async def run():
        mock_client = MockBlockchainClient()
        agent = BlockchainExecutionAgent(blockchain_client=mock_client)
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id, operation="LOCK_CAPACITY")
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        # First execution succeeds
        res1 = await agent.execute(msg)
        assert res1.status == AgentStatus.COMPLETED

        # Second execution with exact same payload/key must be rejected
        res2 = await agent.execute(msg)
        assert res2.status == AgentStatus.FAILED
        assert res2.error is not None
        assert "duplicate transaction rejected" in res2.error.message.lower()
        # Verify client was called only once
        assert len(mock_client.calls) == 1

    asyncio.run(run())


def test_preserves_workflow_to_transaction_mapping():
    """Audit Invariant: Preserve direct mapping from workflow ID to on-chain transaction hash."""
    async def run():
        mock_client = MockBlockchainClient()
        agent = BlockchainExecutionAgent(blockchain_client=mock_client)
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id)
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.COMPLETED
        assert result.data["workflow_id"] == str(wf_id)
        assert result.data["tx_hash"] == mock_client.lock_response["txHash"]
        assert result.data["block_number"] == mock_client.lock_response["blockNumber"]

    asyncio.run(run())


def test_does_not_expose_private_keys():
    """Security Invariant: Agent rejects any private key or secret parameters."""
    async def run():
        agent = BlockchainExecutionAgent(blockchain_client=MockBlockchainClient())
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id)
        payload["private_key"] = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "forbidden key" in result.error.message.lower() or "security violation" in result.error.message.lower()

    asyncio.run(run())


def test_does_not_report_success_before_confirmation():
    """Receipt Invariant: Transaction without a confirmed txHash is never reported as success."""
    async def run():
        # Response missing txHash
        unconfirmed_client = MockBlockchainClient(
            lock_response={"success": True, "status": "pending"}
        )
        agent = BlockchainExecutionAgent(blockchain_client=unconfirmed_client)
        wf_id = uuid4()
        payload = make_valid_payload(workflow_id=wf_id)
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="BlockchainExecutionAgent",
            message_type="EXECUTE_COMMITMENT",
            payload=payload,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert "unconfirmed" in result.error.message.lower() or "missing transaction hash" in result.error.message.lower()

    asyncio.run(run())
