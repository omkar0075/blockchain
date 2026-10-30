import asyncio
import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional, Union
from uuid import UUID

from .base import BaseAgent
from .registry import AgentRegistry, default_registry
from ..schemas.agent_messages import (
    AgentError,
    AgentMessage,
    AgentResult,
    AgentStatus,
)

logger = logging.getLogger("medway.agents.bus")

SubscriberHandler = Union[BaseAgent, Callable[[AgentMessage], Awaitable[AgentResult]]]


class MessageBus:
    """
    In-process asynchronous message bus for decoupled multi-agent coordination.
    
    Architectural & Delivery Invariants:
    1. Structured Messaging: Agents communicate solely via AgentMessage envelopes.
    2. Recipient Validation: Messages addressed to unregistered/unknown agents are rejected.
    3. Deduplication: Duplicate processing is prevented using message_id.
    4. Fault Isolation: Unhandled exceptions from individual subscribers are captured
       as compliant FAILED AgentResults, preventing workflow-wide crashes.
    5. Workflow Correlation: Retains workflow_id lineage and isolation.
    """

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        max_dedup_cache_size: int = 5000,
    ) -> None:
        self.registry: AgentRegistry = registry if registry is not None else default_registry
        self._subscribers: dict[str, list[SubscriberHandler]] = {}
        self._processed_message_ids: OrderedDict[UUID, bool] = OrderedDict()
        self._max_dedup_cache_size = max_dedup_cache_size
        self._history: dict[UUID, list[AgentMessage]] = {}
        self._results: dict[UUID, list[AgentResult]] = {}
        self._lock = asyncio.Lock()

    def subscribe(self, receiver: str, handler: SubscriberHandler) -> None:
        """
        Register a subscriber (BaseAgent or async callable) for a specific receiver name or '*'.
        
        Args:
            receiver: Target agent name or '*' for broadcast.
            handler: BaseAgent instance or async function taking AgentMessage -> AgentResult.
        """
        cleaned = receiver.strip() if receiver else ""
        if not cleaned:
            raise ValueError("Receiver name cannot be empty or whitespace only")

        key = cleaned.lower()
        if key not in self._subscribers:
            self._subscribers[key] = []
        if handler not in self._subscribers[key]:
            self._subscribers[key].append(handler)
            logger.info("Subscribed handler '%s' to topic/receiver '%s'", getattr(handler, "name", str(handler)), cleaned)

    def unsubscribe(self, receiver: str, handler: SubscriberHandler) -> bool:
        """Remove a subscriber from a receiver topic."""
        key = receiver.strip().lower()
        if key in self._subscribers and handler in self._subscribers[key]:
            self._subscribers[key].remove(handler)
            if not self._subscribers[key]:
                del self._subscribers[key]
            return True
        return False

    def clear(self) -> None:
        """Reset subscribers, deduplication cache, and history."""
        self._subscribers.clear()
        self._processed_message_ids.clear()
        self._history.clear()
        self._results.clear()

    def get_workflow_messages(self, workflow_id: UUID) -> list[AgentMessage]:
        """Retrieve all messages published under a specific workflow_id."""
        return list(self._history.get(workflow_id, []))

    def get_workflow_results(self, workflow_id: UUID) -> list[AgentResult]:
        """Retrieve all execution results collected under a specific workflow_id."""
        return list(self._results.get(workflow_id, []))

    async def publish(self, message: AgentMessage) -> list[AgentResult]:
        """
        Publish an AgentMessage to its intended recipient(s).
        
        1. Validates envelope and recipient.
        2. Detects and rejects duplicate message_id.
        3. Identifies matching handlers from explicit subscribers and AgentRegistry.
        4. Delivers asynchronously, capturing errors gracefully.
        5. Logs delivery metrics and returns all AgentResult outcomes.
        """
        if not isinstance(message, AgentMessage):
            raise TypeError(f"Expected AgentMessage, got {type(message).__name__}")

        # 1. Deduplication Check
        async with self._lock:
            if message.message_id in self._processed_message_ids:
                raise ValueError(
                    f"Duplicate message rejected: message_id {message.message_id} has already been processed"
                )
            self._processed_message_ids[message.message_id] = True
            # Maintain bounded LRU cache
            if len(self._processed_message_ids) > self._max_dedup_cache_size:
                self._processed_message_ids.popitem(last=False)

            # Record in workflow history
            if message.workflow_id not in self._history:
                self._history[message.workflow_id] = []
            self._history[message.workflow_id].append(message)

        # 2. Recipient Resolution
        target_name = message.receiver.strip()
        target_key = target_name.lower()
        handlers: list[tuple[str, SubscriberHandler]] = []

        if target_name == "*":
            # Broadcast to all registered subscribers
            for key, sub_list in self._subscribers.items():
                for sub in sub_list:
                    handlers.append((key, sub))
        else:
            # Check explicit subscribers
            if target_key in self._subscribers:
                for sub in self._subscribers[target_key]:
                    handlers.append((target_name, sub))

            # Check registry if no explicit subscriber matched
            if not handlers and self.registry.has(target_name):
                handlers.append((target_name, self.registry.get(target_name)))

            # Check global broadcast subscribers
            if "*" in self._subscribers:
                for sub in self._subscribers["*"]:
                    handlers.append((target_name, sub))

            # Reject unknown receivers cleanly
            if not handlers:
                err_msg = f"Cannot deliver message {message.message_id}: unknown receiver '{target_name}'"
                logger.error(err_msg)
                raise KeyError(err_msg)

        logger.info(
            "Publishing message %s (type=%s, workflow=%s) to %d handler(s) for '%s'",
            message.message_id,
            message.message_type,
            message.workflow_id,
            len(handlers),
            target_name,
        )

        # 3. Asynchronous Concurrent Delivery
        results: list[AgentResult] = []
        delivery_tasks = [
            self._safe_deliver(recipient_name, handler, message)
            for recipient_name, handler in handlers
        ]

        delivery_outcomes = await asyncio.gather(*delivery_tasks, return_exceptions=True)

        for recipient_name, outcome in zip([h[0] for h in handlers], delivery_outcomes):
            if isinstance(outcome, AgentResult):
                results.append(outcome)
            elif isinstance(outcome, Exception):
                logger.exception("Unexpected exception in handler for '%s': %s", recipient_name, outcome)
                results.append(
                    AgentResult(
                        agent_name=recipient_name,
                        workflow_id=message.workflow_id,
                        status=AgentStatus.FAILED,
                        data={},
                        execution_time=0.0,
                        error=AgentError(
                            error_code=outcome.__class__.__name__,
                            message=str(outcome) or "Unknown delivery error",
                            agent_name=recipient_name,
                            workflow_id=message.workflow_id,
                            recoverable=False,
                        ),
                    )
                )

        # 4. Store workflow results
        async with self._lock:
            if message.workflow_id not in self._results:
                self._results[message.workflow_id] = []
            self._results[message.workflow_id].extend(results)

        return results

    async def _safe_deliver(
        self,
        recipient_name: str,
        handler: SubscriberHandler,
        message: AgentMessage,
    ) -> AgentResult:
        """Deliver message to a single handler safely catching unhandled exceptions."""
        try:
            if isinstance(handler, BaseAgent):
                return await handler.execute(message)
            elif callable(handler):
                result = await handler(message)
                if not isinstance(result, AgentResult):
                    raise TypeError(
                        f"Custom subscriber must return AgentResult, got {type(result).__name__}"
                    )
                return result
            else:
                raise TypeError(f"Invalid handler type: {type(handler).__name__}")

        except Exception as exc:
            logger.exception("Delivery failure to recipient '%s': %s", recipient_name, exc)
            return AgentResult(
                agent_name=recipient_name,
                workflow_id=message.workflow_id,
                status=AgentStatus.FAILED,
                data={},
                execution_time=0.0,
                error=AgentError(
                    error_code=exc.__class__.__name__,
                    message=str(exc) or "Handler execution failed",
                    agent_name=recipient_name,
                    workflow_id=message.workflow_id,
                    recoverable=getattr(exc, "recoverable", False),
                ),
            )


# Global default message bus
default_bus = MessageBus()
