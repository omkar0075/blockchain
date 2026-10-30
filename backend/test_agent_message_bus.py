import asyncio
from typing import Any, Optional
from uuid import UUID, uuid4
import pytest

from app.agents.base import BaseAgent
from app.agents.message_bus import MessageBus
from app.agents.registry import AgentRegistry
from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
)


class MockWorkerAgent(BaseAgent):
    def __init__(self, name: str, return_data: Optional[dict] = None, should_fail: bool = False):
        super().__init__(name)
        self.return_data = return_data or {"handled": True}
        self.should_fail = should_fail
        self.received_messages: list[AgentMessage] = []

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        self.received_messages.append(message)
        if self.should_fail:
            raise RuntimeError(f"Simulated handler failure in {self.name}")
        return {**self.return_data, "msg_type": message.message_type}


def make_message(sender: str, receiver: str, workflow_id: Optional[UUID] = None, msg_type: str = "TEST_EVENT") -> AgentMessage:
    return AgentMessage(
        workflow_id=workflow_id or uuid4(),
        sender=sender,
        receiver=receiver,
        message_type=msg_type,
        payload={"sample_key": "sample_val"},
    )


def test_successful_direct_message_delivery():
    """Verify single receiver delivery via explicit subscriber or AgentRegistry."""
    async def run():
        registry = AgentRegistry()
        bus = MessageBus(registry=registry)
        agent = MockWorkerAgent("DiscoveryAgent")
        registry.register(agent)

        wf_id = uuid4()
        msg = make_message(sender="RequestAgent", receiver="DiscoveryAgent", workflow_id=wf_id)

        results = await bus.publish(msg)

        assert len(results) == 1
        assert results[0].agent_name == "DiscoveryAgent"
        assert results[0].status == AgentStatus.COMPLETED
        assert results[0].workflow_id == wf_id
        assert len(agent.received_messages) == 1
        assert agent.received_messages[0].message_id == msg.message_id

    asyncio.run(run())


def test_multiple_subscribers():
    """Verify broadcast '*' or multiple handlers receiving the message concurrently."""
    async def run():
        bus = MessageBus()
        agent1 = MockWorkerAgent("SubscriberOne")
        agent2 = MockWorkerAgent("SubscriberTwo")

        bus.subscribe("TopicAlpha", agent1)
        bus.subscribe("TopicAlpha", agent2)

        msg = make_message(sender="Coordinator", receiver="TopicAlpha")
        results = await bus.publish(msg)

        assert len(results) == 2
        assert {r.agent_name for r in results} == {"SubscriberOne", "SubscriberTwo"}
        assert all(r.status == AgentStatus.COMPLETED for r in results)

    asyncio.run(run())


def test_unknown_receiver_rejection():
    """Verify attempting to publish to an unregistered recipient cleanly raises KeyError."""
    async def run():
        bus = MessageBus()
        msg = make_message(sender="RequestAgent", receiver="GhostAgent")

        with pytest.raises(KeyError, match="Cannot deliver message .* unknown receiver 'GhostAgent'"):
            await bus.publish(msg)

    asyncio.run(run())


def test_duplicate_message_rejection():
    """Verify message_id deduplication prevents duplicate processing."""
    async def run():
        registry = AgentRegistry()
        bus = MessageBus(registry=registry)
        agent = MockWorkerAgent("TargetAgent")
        registry.register(agent)

        msg = make_message(sender="Sender", receiver="TargetAgent")

        # First publish: succeeds
        results1 = await bus.publish(msg)
        assert len(results1) == 1
        assert results1[0].status == AgentStatus.COMPLETED

        # Second publish of the same message_id: rejected
        with pytest.raises(ValueError, match="Duplicate message rejected: message_id .* has already been processed"):
            await bus.publish(msg)

    asyncio.run(run())


def test_workflow_isolation():
    """Verify messages and execution results are accurately isolated by workflow_id."""
    async def run():
        registry = AgentRegistry()
        bus = MessageBus(registry=registry)
        agent = MockWorkerAgent("Worker")
        registry.register(agent)

        wf_a = uuid4()
        wf_b = uuid4()

        msg_a1 = make_message("Req", "Worker", workflow_id=wf_a, msg_type="A_EVENT_1")
        msg_a2 = make_message("Req", "Worker", workflow_id=wf_a, msg_type="A_EVENT_2")
        msg_b1 = make_message("Req", "Worker", workflow_id=wf_b, msg_type="B_EVENT_1")

        await bus.publish(msg_a1)
        await bus.publish(msg_b1)
        await bus.publish(msg_a2)

        messages_a = bus.get_workflow_messages(wf_a)
        messages_b = bus.get_workflow_messages(wf_b)

        assert len(messages_a) == 2
        assert len(messages_b) == 1
        assert messages_a[0].message_type == "A_EVENT_1"
        assert messages_a[1].message_type == "A_EVENT_2"
        assert messages_b[0].message_type == "B_EVENT_1"

        assert len(bus.get_workflow_results(wf_a)) == 2
        assert len(bus.get_workflow_results(wf_b)) == 1

    asyncio.run(run())


def test_subscriber_failure_handling():
    """Verify that a failing subscriber returns a FAILED result without crashing other handlers."""
    async def run():
        bus = MessageBus()
        success_agent = MockWorkerAgent("GoodAgent", should_fail=False)
        failing_agent = MockWorkerAgent("BadAgent", should_fail=True)

        bus.subscribe("Cluster", success_agent)
        bus.subscribe("Cluster", failing_agent)

        msg = make_message("Sender", "Cluster")
        results = await bus.publish(msg)

        assert len(results) == 2
        good_res = next(r for r in results if r.agent_name == "GoodAgent")
        bad_res = next(r for r in results if r.agent_name == "BadAgent")

        assert good_res.status == AgentStatus.COMPLETED
        assert bad_res.status == AgentStatus.FAILED
        assert bad_res.error is not None
        assert "Simulated handler failure" in bad_res.error.message

    asyncio.run(run())


def test_asynchronous_concurrent_publishing():
    """Verify concurrent publishers submitting distinct messages concurrently."""
    async def run():
        registry = AgentRegistry()
        bus = MessageBus(registry=registry)
        agent = MockWorkerAgent("ParallelAgent")
        registry.register(agent)

        wf_id = uuid4()
        messages = [
            make_message("Sender", "ParallelAgent", workflow_id=wf_id, msg_type=f"PARALLEL_{i}")
            for i in range(15)
        ]

        delivery_batches = await asyncio.gather(*(bus.publish(m) for m in messages))

        assert len(delivery_batches) == 15
        assert all(len(b) == 1 and b[0].status == AgentStatus.COMPLETED for b in delivery_batches)
        assert len(bus.get_workflow_messages(wf_id)) == 15

    asyncio.run(run())


def test_message_ordering_within_workflow():
    """Verify serial message publishing maintains arrival order in workflow history."""
    async def run():
        registry = AgentRegistry()
        bus = MessageBus(registry=registry)
        agent = MockWorkerAgent("OrderAgent")
        registry.register(agent)

        wf_id = uuid4()
        for seq in range(5):
            await bus.publish(make_message("Sender", "OrderAgent", workflow_id=wf_id, msg_type=f"SEQ_{seq}"))

        history = bus.get_workflow_messages(wf_id)
        assert len(history) == 5
        for seq in range(5):
            assert history[seq].message_type == f"SEQ_{seq}"

    asyncio.run(run())
