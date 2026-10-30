import abc
import logging
import time
from typing import Any, Optional
from uuid import UUID

from ..schemas.agent_messages import (
    AgentError,
    AgentMessage,
    AgentResult,
    AgentStatus,
)


class BaseAgent(abc.ABC):
    """
    Abstract base class for all MEDway autonomous agents.
    
    Standardizes lifecycle management, message handling, error capture,
    and high-precision performance monitoring across the multi-agent network.
    """

    def __init__(self, name: str) -> None:
        stripped_name = name.strip() if name else ""
        if not stripped_name:
            raise ValueError("Agent name cannot be empty or whitespace only")

        self.name: str = stripped_name
        self.status: AgentStatus = AgentStatus.PENDING
        self.logger: logging.Logger = logging.getLogger(f"medway.agents.{self.name}")

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.name} (status={self.status.value})>"

    def reset(self) -> None:
        """Reset the agent to its initial pending state."""
        self.status = AgentStatus.PENDING

    def set_status(self, status: AgentStatus) -> None:
        """Explicitly update the agent's execution status."""
        self.status = status

    def create_message(
        self,
        receiver: str,
        message_type: str,
        payload: dict[str, Any],
        workflow_id: UUID,
    ) -> AgentMessage:
        """Helper to construct a valid outgoing AgentMessage from this agent."""
        return AgentMessage(
            workflow_id=workflow_id,
            sender=self.name,
            receiver=receiver,
            message_type=message_type,
            payload=payload,
        )

    def create_error(
        self,
        workflow_id: UUID,
        error_code: str,
        message: str,
        recoverable: bool = False,
    ) -> AgentError:
        """Helper to construct a standardized AgentError for this agent."""
        return AgentError(
            error_code=error_code,
            message=message,
            agent_name=self.name,
            workflow_id=workflow_id,
            recoverable=recoverable,
        )

    async def execute(self, message: AgentMessage) -> AgentResult:
        """
        Execute the agent workflow against an incoming AgentMessage.
        
        Template method that:
        1. Validates message structure and recipient.
        2. Measures execution duration with high precision.
        3. Updates status to RUNNING.
        4. Delegates business logic to concrete process() method.
        5. Catches any unhandled exceptions and wraps them in a standardized AgentError.
        6. Produces a compliant AgentResult.
        """
        if not isinstance(message, AgentMessage):
            raise TypeError(f"Expected AgentMessage instance, got {type(message).__name__}")

        # Recipient check: allow direct address, broadcast '*', or case-insensitive match
        if message.receiver != "*" and message.receiver.lower() != self.name.lower():
            self.logger.warning(
                "Message receiver '%s' does not match agent '%s'",
                message.receiver,
                self.name,
            )

        start_time = time.perf_counter()
        self.status = AgentStatus.RUNNING
        self.logger.info(
            "Agent '%s' started executing workflow %s (type=%s)",
            self.name,
            message.workflow_id,
            message.message_type,
        )

        try:
            data = await self.process(message)
            if not isinstance(data, dict):
                raise TypeError(f"process() must return a dict, got {type(data).__name__}")

            # Keep WAITING_FOR_APPROVAL if set during process, otherwise transition to COMPLETED
            if self.status != AgentStatus.WAITING_FOR_APPROVAL:
                self.status = AgentStatus.COMPLETED

            execution_time = max(0.0, time.perf_counter() - start_time)
            self.logger.info(
                "Agent '%s' completed workflow %s in %.4fs (status=%s)",
                self.name,
                message.workflow_id,
                execution_time,
                self.status.value,
            )

            return AgentResult(
                agent_name=self.name,
                workflow_id=message.workflow_id,
                status=self.status,
                data=data,
                execution_time=execution_time,
                error=None,
            )

        except Exception as exc:
            self.status = AgentStatus.FAILED
            execution_time = max(0.0, time.perf_counter() - start_time)
            self.logger.exception(
                "Agent '%s' failed workflow %s after %.4fs: %s",
                self.name,
                message.workflow_id,
                execution_time,
                str(exc),
            )

            error = self.create_error(
                workflow_id=message.workflow_id,
                error_code=exc.__class__.__name__,
                message=str(exc) or "Unknown agent execution error",
                recoverable=getattr(exc, "recoverable", False),
            )

            return AgentResult(
                agent_name=self.name,
                workflow_id=message.workflow_id,
                status=AgentStatus.FAILED,
                data={},
                execution_time=execution_time,
                error=error,
            )

    @abc.abstractmethod
    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Concrete processing logic implemented by each specialized agent.
        
        Must return a dict containing the output payload.
        """
        raise NotImplementedError("Subclasses must implement process()")

