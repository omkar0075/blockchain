import asyncio
import threading
from typing import Any
import pytest

from app.agents.base import BaseAgent
from app.agents.registry import (
    AgentRegistry,
    clear_registry,
    default_registry,
    get_agent,
    list_agents,
    register_agent,
)
from app.schemas.agent_messages import AgentMessage


class SampleAgent(BaseAgent):
    def __init__(self, name: str):
        super().__init__(name)

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        return {"handled_by": self.name}


def test_register_and_retrieve_agent():
    """Verify registering an agent and retrieving it by name (case-insensitive)."""
    registry = AgentRegistry()
    agent = SampleAgent("DiscoveryAgent")

    registry.register(agent)

    # Retrieval matching exact case
    assert registry.get("DiscoveryAgent") is agent
    # Case-insensitive retrieval
    assert registry.get("discoveryagent") is agent
    assert registry.get("DISCOVERYAGENT") is agent
    assert registry.has("DiscoveryAgent") is True
    assert "DiscoveryAgent" in registry
    assert len(registry) == 1


def test_reject_non_base_agent():
    """Verify registry strictly enforces BaseAgent inheritance."""
    registry = AgentRegistry()
    with pytest.raises(TypeError, match="Expected BaseAgent instance"):
        registry.register("not an agent")  # type: ignore

    with pytest.raises(TypeError, match="Expected BaseAgent instance"):
        registry.register({"name": "FakeAgent"})  # type: ignore


def test_reject_duplicate_registration():
    """Verify duplicate registrations (case-insensitive) are strictly rejected."""
    registry = AgentRegistry()
    agent1 = SampleAgent("NegotiationAgent")
    agent2 = SampleAgent("negotiationagent")

    registry.register(agent1)

    with pytest.raises(ValueError, match="is already registered"):
        registry.register(agent1)

    with pytest.raises(ValueError, match="is already registered"):
        registry.register(agent2)


def test_retrieve_unknown_agent_raises_clean_key_error():
    """Verify clean KeyError when retrieving an unknown agent."""
    registry = AgentRegistry()
    registry.register(SampleAgent("RequestAgent"))

    with pytest.raises(KeyError, match="Agent 'UnknownAgent' is not registered. Available agents: \\['RequestAgent'\\]"):
        registry.get("UnknownAgent")

    with pytest.raises(ValueError, match="Agent name cannot be empty or whitespace only"):
        registry.get("   ")


def test_list_agents_and_details():
    """Verify list_agents() and list_details() output structure and sorting."""
    registry = AgentRegistry()
    registry.register(SampleAgent("VerificationAgent"))
    registry.register(SampleAgent("BlockchainAgent"))
    registry.register(SampleAgent("MonitoringAgent"))

    names = registry.list_agents()
    assert names == ["BlockchainAgent", "MonitoringAgent", "VerificationAgent"]

    details = registry.list_details()
    assert len(details) == 3
    assert details[0]["name"] == "BlockchainAgent"
    assert details[0]["class"] == "SampleAgent"
    assert details[0]["status"] == "PENDING"


def test_unregister_agent():
    """Verify unregistering removes the agent and subsequent lookup fails."""
    registry = AgentRegistry()
    agent = SampleAgent("RecoveryAgent")
    registry.register(agent)
    assert len(registry) == 1

    removed = registry.unregister("RecoveryAgent")
    assert removed is agent
    assert len(registry) == 0
    assert registry.has("RecoveryAgent") is False

    with pytest.raises(KeyError, match="Cannot unregister: agent 'RecoveryAgent' is not registered"):
        registry.unregister("RecoveryAgent")


def test_clear_registry():
    """Verify clearing the registry resets its state."""
    registry = AgentRegistry()
    registry.register(SampleAgent("AgentA"))
    registry.register(SampleAgent("AgentB"))
    assert len(registry) == 2

    registry.clear()
    assert len(registry) == 0
    assert registry.list_agents() == []


def test_concurrent_thread_safety():
    """Verify concurrent threads registering and querying agents without race conditions."""
    registry = AgentRegistry()
    errors: list[Exception] = []

    def worker(worker_id: int):
        try:
            agent = SampleAgent(f"ConcurrentAgent_{worker_id}")
            registry.register(agent)
            retrieved = registry.get(f"concurrentagent_{worker_id}")
            assert retrieved is agent
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(registry) == 20


def test_module_level_default_registry_helpers():
    """Verify module-level default_registry helper functions."""
    clear_registry()
    try:
        agent = SampleAgent("GlobalAgent")
        register_agent(agent)
        assert get_agent("GlobalAgent") is agent
        assert "GlobalAgent" in list_agents()
    finally:
        clear_registry()
