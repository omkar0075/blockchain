import logging
import threading
from typing import Any, Optional

from .base import BaseAgent

logger = logging.getLogger("medway.agents.registry")


class AgentRegistry:
    """
    Thread-safe and async-safe catalog of autonomous agents in the MEDway network.
    
    Security & Architectural Invariants:
    1. Zero Privilege Elevation: The registry is strictly an in-memory execution
       dispatch table mapping names to BaseAgent instances. Registration does NOT
       grant or alter institutional roles, API keys, or blockchain permissions.
    2. Strict Uniqueness: Duplicate registrations are rejected.
    3. Type Enforcement: Only validated BaseAgent instances are accepted.
    """

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}
        self._lock = threading.RLock()

    def register(self, agent: BaseAgent) -> None:
        """
        Register a BaseAgent instance by its unique name.
        
        Raises:
            TypeError: If agent is not an instance of BaseAgent.
            ValueError: If agent name is empty or already registered.
        """
        if not isinstance(agent, BaseAgent):
            raise TypeError(f"Expected BaseAgent instance, got {type(agent).__name__}")

        name = agent.name.strip() if agent.name else ""
        if not name:
            raise ValueError("Agent name cannot be empty or whitespace only")

        key = name.lower()

        with self._lock:
            if key in self._agents:
                existing = self._agents[key]
                raise ValueError(
                    f"Agent '{name}' is already registered (existing: <{existing.__class__.__name__}: {existing.name}>)"
                )
            self._agents[key] = agent
            logger.info("Registered agent '%s' (%s)", name, agent.__class__.__name__)

    def get(self, name: str) -> BaseAgent:
        """
        Retrieve a registered agent by name (case-insensitive).
        
        Raises:
            ValueError: If name is empty.
            KeyError: If agent is not found.
        """
        cleaned = name.strip() if name else ""
        if not cleaned:
            raise ValueError("Agent name cannot be empty or whitespace only")

        key = cleaned.lower()

        with self._lock:
            if key not in self._agents:
                available = sorted([a.name for a in self._agents.values()])
                raise KeyError(
                    f"Agent '{name}' is not registered. Available agents: {available}"
                )
            return self._agents[key]

    def has(self, name: str) -> bool:
        """Check whether an agent name is currently registered."""
        if not name or not name.strip():
            return False
        with self._lock:
            return name.strip().lower() in self._agents

    def list_agents(self) -> list[str]:
        """Return a sorted list of registered agent names."""
        with self._lock:
            return sorted([agent.name for agent in self._agents.values()])

    def list_details(self) -> list[dict[str, Any]]:
        """Return structural inspection metadata for all registered agents."""
        with self._lock:
            return [
                {
                    "name": agent.name,
                    "class": agent.__class__.__name__,
                    "status": agent.status.value,
                }
                for agent in sorted(self._agents.values(), key=lambda a: a.name)
            ]

    def unregister(self, name: str) -> BaseAgent:
        """
        Remove an agent from the registry by name.
        
        Raises:
            KeyError: If agent is not found.
        """
        cleaned = name.strip() if name else ""
        if not cleaned:
            raise ValueError("Agent name cannot be empty or whitespace only")

        key = cleaned.lower()

        with self._lock:
            if key not in self._agents:
                raise KeyError(f"Cannot unregister: agent '{name}' is not registered.")
            removed = self._agents.pop(key)
            logger.info("Unregistered agent '%s'", removed.name)
            return removed

    def clear(self) -> None:
        """Clear all registered agents from the registry."""
        with self._lock:
            count = len(self._agents)
            self._agents.clear()
            logger.debug("Cleared %d agents from registry", count)

    def __len__(self) -> int:
        with self._lock:
            return len(self._agents)

    def __contains__(self, name: str) -> bool:
        return self.has(name)


# Global default registry instance for MEDway orchestrator
default_registry = AgentRegistry()


def register_agent(agent: BaseAgent) -> None:
    """Helper to register an agent in the global default registry."""
    default_registry.register(agent)


def get_agent(name: str) -> BaseAgent:
    """Helper to retrieve an agent from the global default registry."""
    return default_registry.get(name)


def list_agents() -> list[str]:
    """Helper to list all agents registered in the global default registry."""
    return default_registry.list_agents()


def clear_registry() -> None:
    """Helper to clear the global default registry."""
    default_registry.clear()
