import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.agents.monitoring_agent import (
    BlockchainEvent,
    MonitoringAgent,
    ReservationLifecycleState,
)
from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)


class MockBlockchainReader:
    """Mock reader simulating RPC receipts and contract state lookups."""

    def __init__(
        self,
        receipt_response: Optional[dict[str, Any]] = None,
        lock_response: Optional[dict[str, Any]] = None,
        reservation_response: Optional[dict[str, Any]] = None,
        raise_rpc_error: bool = False,
    ) -> None:
        self.receipt_response = receipt_response or {
            "success": True,
            "status": "confirmed",
            "blockNumber": 1285000,
            "gasUsed": 85000,
        }
        self.lock_response = lock_response or {
            "success": True,
            "lockId": "0xlock123",
            "status": "Active",
            "expiresAt": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        }
        self.reservation_response = reservation_response or {
            "success": True,
            "reservationId": "0xres123",
            "status": "Committed",
            "fulfilledAt": 0,
        }
        self.raise_rpc_error = raise_rpc_error

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        if self.raise_rpc_error:
            raise ConnectionError("RPC provider unreachable: https://coralrpc.mstblockchain.com/")
        return self.receipt_response

    def get_lock(self, lock_id: str) -> dict[str, Any]:
        if self.raise_rpc_error:
            raise ConnectionError("RPC provider unreachable")
        return self.lock_response

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        if self.raise_rpc_error:
            raise ConnectionError("RPC provider unreachable")
        return self.reservation_response


def test_detects_successful_transaction_confirmation():
    """Verify detecting and recording a confirmed blockchain transaction receipt."""
    async def run():
        mock_reader = MockBlockchainReader(
            receipt_response={"success": True, "status": "confirmed", "blockNumber": 1285000, "gasUsed": 85000}
        )
        agent = MonitoringAgent(reader=mock_reader)
        wf_id = uuid4()
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={"tx_hash": "0x4a9b6c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b"},
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is True
        assert result.data["status"] == "CONFIRMED"
        assert result.data["confirmed"] is True
        assert result.data["block_number"] == 1285000
        assert result.data["workflow_id"] == str(wf_id)

    asyncio.run(run())


def test_detects_failed_transaction():
    """Verify that a reverted transaction receipt is detected and flagged."""
    async def run():
        mock_reader = MockBlockchainReader(
            receipt_response={
                "success": True,
                "status": "reverted",
                "blockNumber": 1285002,
                "revertReason": "Execution reverted: Insufficient uncommitted capacity",
            }
        )
        agent = MonitoringAgent(reader=mock_reader)
        wf_id = uuid4()
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={"tx_hash": "0xrevertedtx1234567890"},
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.COMPLETED
        assert result.data["success"] is False
        assert result.data["status"] == "REVERTED"
        assert result.data["confirmed"] is True
        assert "insufficient uncommitted capacity" in result.data["error"].lower()

    asyncio.run(run())


def test_processes_reservation_events():
    """Verify lifecycle state transitions triggered by authoritative on-chain events."""
    async def run():
        agent = MonitoringAgent()
        wf_id = uuid4()
        res_id = "0xres_event_test_1"

        # 1. CapacityLocked event
        event1 = BlockchainEvent(
            event_name="CapacityLocked",
            transaction_hash="0xtx1",
            block_number=100,
            payload={"lockId": res_id, "quantity": 2, "expiresAt": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())},
        )
        msg1 = AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=event1.model_dump(),
        )
        res1 = await agent.execute(msg1)
        assert res1.data["state"] == "LOCKED"

        # 2. ReservationCommitted event
        event2 = BlockchainEvent(
            event_name="ReservationCommitted",
            transaction_hash="0xtx2",
            block_number=105,
            payload={"reservationId": res_id},
        )
        msg2 = AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=event2.model_dump(),
        )
        res2 = await agent.execute(msg2)
        assert res2.data["state"] == "COMMITTED"

        # 3. ReservationDisputed event
        event3 = BlockchainEvent(
            event_name="ReservationDisputed",
            transaction_hash="0xtx3",
            block_number=110,
            payload={"reservationId": res_id, "evidenceHash": "0xevidence123"},
        )
        msg3 = AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=event3.model_dump(),
        )
        res3 = await agent.execute(msg3)
        assert res3.data["state"] == "DISPUTED"

    asyncio.run(run())


def test_ignores_duplicate_events():
    """Deduplication Invariant: Identical events must not be processed more than once."""
    async def run():
        agent = MonitoringAgent()
        wf_id = uuid4()
        event = BlockchainEvent(
            event_name="ReservationCommitted",
            transaction_hash="0xduplicate_tx_hash_1",
            block_number=200,
            log_index=1,
            payload={"reservationId": "0xres_dup_1"},
        )
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=event.model_dump(),
        )

        # First dispatch
        res1 = await agent.execute(msg)
        assert res1.data["processed"] is True
        assert res1.data["duplicate"] is False

        # Duplicate dispatch
        res2 = await agent.execute(msg)
        assert res2.data["processed"] is False
        assert res2.data["duplicate"] is True
        assert "duplicate" in res2.data["message"].lower()

    asyncio.run(run())


def test_detects_expired_locks():
    """Verify that locks exceeding their expiry timestamp are detected and transitioned to EXPIRED."""
    async def run():
        agent = MonitoringAgent()
        wf_id = uuid4()
        now = datetime.now(timezone.utc)
        past_expiry = now - timedelta(minutes=5)

        # Inject an active lock that expired 5 minutes ago
        lock_id = "0xexpired_lock_99"
        event = BlockchainEvent(
            event_name="CapacityLocked",
            transaction_hash="0xtx_exp_1",
            block_number=300,
            payload={"lockId": lock_id, "expiresAt": int(past_expiry.timestamp())},
        )
        await agent.execute(AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=event.model_dump(),
        ))

        # Check deadlines
        check_msg = AgentMessage(
            workflow_id=wf_id,
            sender="Scheduler",
            receiver="MonitoringAgent",
            message_type="CHECK_DEADLINES",
            payload={},
        )
        res = await agent.execute(check_msg)

        assert lock_id in res.data["expired_locks"]
        # Entity state must now be EXPIRED
        entity = agent._entities[lock_id]
        assert entity.state == ReservationLifecycleState.EXPIRED

    asyncio.run(run())


def test_detects_approaching_deadlines():
    """Verify that locks approaching their expiry window trigger proactive warnings."""
    async def run():
        # warning threshold 30 minutes
        agent = MonitoringAgent(deadline_warning_seconds=1800)
        wf_id = uuid4()
        now = datetime.now(timezone.utc)
        # Lock expires in 15 minutes (within 30 minute window)
        approaching_time = now + timedelta(minutes=15)
        lock_id = "0xurgent_lock_55"

        event = BlockchainEvent(
            event_name="CapacityLocked",
            transaction_hash="0xtx_urgent_1",
            block_number=400,
            payload={"lockId": lock_id, "expiresAt": int(approaching_time.timestamp())},
        )
        await agent.execute(AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=event.model_dump(),
        ))

        check_msg = AgentMessage(
            workflow_id=wf_id,
            sender="Scheduler",
            receiver="MonitoringAgent",
            message_type="CHECK_DEADLINES",
            payload={},
        )
        res = await agent.execute(check_msg)

        assert len(res.data["approaching_deadlines"]) >= 1
        found = [d for d in res.data["approaching_deadlines"] if d["entity_id"] == lock_id]
        assert len(found) == 1
        assert found[0]["remaining_seconds"] > 0
        assert found[0]["remaining_seconds"] <= 1800

    asyncio.run(run())


def test_handles_rpc_disconnection():
    """Verify that RPC failure or disconnection returns clean error status without crashing."""
    async def run():
        failing_reader = MockBlockchainReader(raise_rpc_error=True)
        agent = MonitoringAgent(reader=failing_reader)
        wf_id = uuid4()
        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={"tx_hash": "0xanytx"},
        )

        result = await agent.execute(msg)

        assert result.data["success"] is False
        assert result.data["status"] == "RPC_ERROR"
        assert result.data["confirmed"] is False
        assert "rpc" in result.data["error"].lower()

    asyncio.run(run())


def test_handles_delayed_confirmations():
    """Verify that unconfirmed transactions past the timeout window are flagged as DELAYED."""
    async def run():
        # timeout 60 seconds
        agent = MonitoringAgent(
            reader=MockBlockchainReader(receipt_response={"success": False, "error": "Transaction not yet mined"}),
            confirmation_timeout_seconds=60,
        )
        wf_id = uuid4()
        now = datetime.now(timezone.utc)
        # Submitted 3 minutes ago
        submitted_at = now - timedelta(minutes=3)

        msg = AgentMessage(
            workflow_id=wf_id,
            sender="Orchestrator",
            receiver="MonitoringAgent",
            message_type="CHECK_TRANSACTION",
            payload={
                "tx_hash": "0xslowtx",
                "submitted_at": submitted_at.isoformat(),
            },
        )

        result = await agent.execute(msg)

        assert result.data["confirmed"] is False
        assert result.data["status"] == "DELAYED"
        assert result.data["is_delayed"] is True

    asyncio.run(run())


def test_does_not_falsely_mark_reservations_fulfilled():
    """Proof Invariant: Never mark reservation fulfilled without authoritative evidence."""
    async def run():
        agent = MonitoringAgent()
        wf_id = uuid4()
        res_id = "0xres_unproven_1"

        # Initialize as COMMITTED
        init_event = BlockchainEvent(
            event_name="ReservationCommitted",
            transaction_hash="0xtx_init",
            block_number=500,
            payload={"reservationId": res_id},
        )
        await agent.execute(AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=init_event.model_dump(),
        ))

        # Attempt to fulfill with missing fulfilledAt timestamp
        bad_event = BlockchainEvent(
            event_name="ReservationFulfilled",
            transaction_hash="0xtx_bad_fulfill",
            block_number=505,
            payload={"reservationId": res_id, "fulfilledAt": None},  # No proof!
        )
        res = await agent.execute(AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=bad_event.model_dump(),
        ))

        # State must remain COMMITTED, NOT FULFILLED
        entity = agent._entities[res_id]
        assert entity.state == ReservationLifecycleState.COMMITTED
        assert entity.fulfilled_at is None

    asyncio.run(run())


def test_preserves_workflow_and_transaction_history():
    """Audit Invariant: Preserve full history of state transitions mapped to workflow_id."""
    async def run():
        agent = MonitoringAgent()
        wf_id = uuid4()
        res_id = "0xres_audit_trail_1"

        # Step 1: Locked
        await agent.execute(AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=BlockchainEvent(
                event_name="CapacityLocked",
                transaction_hash="0xtx_a",
                block_number=600,
                payload={"lockId": res_id, "expiresAt": int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())},
            ).model_dump(),
        ))

        # Step 2: Committed
        await agent.execute(AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=BlockchainEvent(
                event_name="ReservationCommitted",
                transaction_hash="0xtx_b",
                block_number=610,
                payload={"reservationId": res_id},
            ).model_dump(),
        ))

        # Step 3: Fulfilled with valid on-chain timestamp
        fulfilled_now = int(datetime.now(timezone.utc).timestamp())
        res = await agent.execute(AgentMessage(
            workflow_id=wf_id,
            sender="EventRelayer",
            receiver="MonitoringAgent",
            message_type="PROCESS_EVENT",
            payload=BlockchainEvent(
                event_name="ReservationFulfilled",
                transaction_hash="0xtx_c",
                block_number=620,
                payload={"reservationId": res_id, "fulfilledAt": fulfilled_now},
            ).model_dump(),
        ))

        assert res.data["state"] == "FULFILLED"
        entity = agent._entities[res_id]
        assert entity.state == ReservationLifecycleState.FULFILLED
        assert len(entity.history) == 3

        # Verify transition sequence
        assert entity.history[0]["from_state"] == "PROPOSED"
        assert entity.history[0]["to_state"] == "LOCKED"
        assert entity.history[1]["from_state"] == "LOCKED"
        assert entity.history[1]["to_state"] == "COMMITTED"
        assert entity.history[2]["from_state"] == "COMMITTED"
        assert entity.history[2]["to_state"] == "FULFILLED"

        # Verify workflow mapping
        assert wf_id in agent._workflow_mappings
        assert res_id in agent._workflow_mappings[wf_id]

    asyncio.run(run())

