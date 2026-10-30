import asyncio
import pytest
from uuid import uuid4
from typing import Any

from app.schemas.agent_messages import (
    AgentMessage,
    AgentResult,
    AgentStatus,
    AgentError,
)
from app.agents.base import BaseAgent


class DummySuccessAgent(BaseAgent):
    async def process(self, message: AgentMessage) -> dict[str, Any]:
        return {"processed": True, "input_payload": message.payload}


class DummyApprovalAgent(BaseAgent):
    async def process(self, message: AgentMessage) -> dict[str, Any]:
        self.set_status(AgentStatus.WAITING_FOR_APPROVAL)
        return {"requires_approval": True}


class RecoverableException(Exception):
    recoverable = True


class DummyFailingAgent(BaseAgent):
    def __init__(self, name: str, should_be_recoverable: bool = False):
        super().__init__(name)
        self.should_be_recoverable = should_be_recoverable

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        if self.should_be_recoverable:
            raise RecoverableException("Transient network timeout")
        raise ValueError("Invalid resource request parameters")


class InvalidOutputAgent(BaseAgent):
    async def process(self, message: AgentMessage) -> Any:
        return "not-a-dict"  # type: ignore


def test_cannot_instantiate_base_agent_directly():
    """Verify BaseAgent cannot be instantiated without implementing process()."""
    with pytest.raises(TypeError):
        BaseAgent("AbstractAgent")  # type: ignore


def test_agent_name_validation():
    """Verify agent name cannot be empty or pure whitespace."""
    with pytest.raises(ValueError, match="Agent name cannot be empty"):
        DummySuccessAgent("")
    with pytest.raises(ValueError, match="Agent name cannot be empty"):
        DummySuccessAgent("   ")


def test_successful_execution_lifecycle():
    """Verify execution transitions, timing, and valid AgentResult output."""
    async def run():
        agent = DummySuccessAgent("RequestAgent")
        assert agent.status == AgentStatus.PENDING

        workflow_id = uuid4()
        msg = agent.create_message(
            receiver="RequestAgent",
            message_type="PROCESS_REQUEST",
            payload={"resource_type": "icu_bed", "quantity": 2},
            workflow_id=workflow_id,
        )

        result = await agent.execute(msg)

        assert isinstance(result, AgentResult)
        assert result.agent_name == "RequestAgent"
        assert result.workflow_id == workflow_id
        assert result.status == AgentStatus.COMPLETED
        assert result.execution_time >= 0.0
        assert result.error is None
        assert result.data == {"processed": True, "input_payload": {"resource_type": "icu_bed", "quantity": 2}}
        assert agent.status == AgentStatus.COMPLETED

        # Test reset
        agent.reset()
        assert agent.status == AgentStatus.PENDING

    asyncio.run(run())


def test_waiting_for_approval_status():
    """Verify an agent can transition to WAITING_FOR_APPROVAL and preserve it."""
    async def run():
        agent = DummyApprovalAgent("ApprovalAgent")
        workflow_id = uuid4()
        msg = agent.create_message(
            receiver="ApprovalAgent",
            message_type="AUTHORIZE_ALLOCATION",
            payload={"reservation_id": "res-123"},
            workflow_id=workflow_id,
        )

        result = await agent.execute(msg)
        assert result.status == AgentStatus.WAITING_FOR_APPROVAL
        assert agent.status == AgentStatus.WAITING_FOR_APPROVAL
        assert result.data["requires_approval"] is True

    asyncio.run(run())


def test_failing_execution_captures_error():
    """Verify exceptions are caught and wrapped cleanly in AgentError within AgentResult."""
    async def run():
        agent = DummyFailingAgent("FailingAgent", should_be_recoverable=False)
        workflow_id = uuid4()
        msg = agent.create_message(
            receiver="FailingAgent",
            message_type="CRASH_TASK",
            payload={},
            workflow_id=workflow_id,
        )

        result = await agent.execute(msg)

        assert result.status == AgentStatus.FAILED
        assert agent.status == AgentStatus.FAILED
        assert result.execution_time >= 0.0
        assert result.data == {}
        assert result.error is not None
        assert result.error.agent_name == "FailingAgent"
        assert result.error.error_code == "ValueError"
        assert "Invalid resource request parameters" in result.error.message
        assert result.error.recoverable is False

    asyncio.run(run())


def test_recoverable_exception_flagging():
    """Verify recoverable exceptions retain their recoverable attribute in AgentError."""
    async def run():
        agent = DummyFailingAgent("TransientAgent", should_be_recoverable=True)
        workflow_id = uuid4()
        msg = agent.create_message(
            receiver="TransientAgent",
            message_type="RETRY_TASK",
            payload={},
            workflow_id=workflow_id,
        )

        result = await agent.execute(msg)
        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert result.error.error_code == "RecoverableException"
        assert result.error.recoverable is True

    asyncio.run(run())


def test_invalid_process_return_type():
    """Verify returning a non-dict raises a TypeError and results in AgentStatus.FAILED."""
    async def run():
        agent = InvalidOutputAgent("BadReturnAgent")
        workflow_id = uuid4()
        msg = agent.create_message(
            receiver="BadReturnAgent",
            message_type="BAD_OUTPUT",
            payload={},
            workflow_id=workflow_id,
        )

        result = await agent.execute(msg)
        assert result.status == AgentStatus.FAILED
        assert result.error is not None
        assert result.error.error_code == "TypeError"
        assert "process() must return a dict" in result.error.message

    asyncio.run(run())


def test_invalid_message_type_rejected():
    """Verify execute() rejects non-AgentMessage input."""
    async def run():
        agent = DummySuccessAgent("TestAgent")
        with pytest.raises(TypeError, match="Expected AgentMessage instance"):
            await agent.execute({"not": "a message"})  # type: ignore

    asyncio.run(run())
