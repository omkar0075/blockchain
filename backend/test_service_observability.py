import json
import sqlite3
import time
from uuid import uuid4
import pytest

from app.db import init_db
from app.services.agent_observability import (
    REDACTED_PII,
    REDACTED_SECRET,
    AgentObservabilityService,
    redact_payload,
    redact_string,
)


@pytest.fixture(autouse=True)
def setup_db():
    init_db()


@pytest.fixture
def obs():
    service = AgentObservabilityService()
    yield service
    service.clear_events()


# -----------------------------------------------------------------------------
# Test 1: Records agent execution events
# -----------------------------------------------------------------------------
def test_records_agent_execution_events(obs):
    wf_id = str(uuid4())

    with obs.track_agent(workflow_id=wf_id, agent_id="DiscoveryAgent", metadata={"query": "diagnostic_slot"}):
        # Simulated agent work
        pass

    events = obs.get_events(workflow_id=wf_id)
    assert len(events) == 2

    start_event = events[0]
    assert start_event["event_type"] == "AGENT_START"
    assert start_event["agent_id"] == "DiscoveryAgent"
    assert start_event["status"] == "STARTED"

    complete_event = events[1]
    assert complete_event["event_type"] == "AGENT_COMPLETE"
    assert complete_event["agent_id"] == "DiscoveryAgent"
    assert complete_event["status"] == "SUCCESS"
    assert complete_event["duration_ms"] is not None


# -----------------------------------------------------------------------------
# Test 2: Tracks execution duration
# -----------------------------------------------------------------------------
def test_tracks_execution_duration(obs):
    wf_id = str(uuid4())

    with obs.track_agent(workflow_id=wf_id, agent_id="NegotiationAgent"):
        time.sleep(0.05)  # 50 ms

    events = obs.get_events(workflow_id=wf_id, event_type="AGENT_COMPLETE")
    assert len(events) == 1
    duration = events[0]["duration_ms"]
    assert duration is not None
    assert duration >= 40.0  # Measured in milliseconds


# -----------------------------------------------------------------------------
# Test 3: Correlates events by workflow ID
# -----------------------------------------------------------------------------
def test_correlates_events_by_workflow_id(obs):
    wf_a = str(uuid4())
    wf_b = str(uuid4())

    obs.record_event(workflow_id=wf_a, agent_id="AgentA", event_type="STEP_1")
    obs.record_event(workflow_id=wf_a, agent_id="AgentA", event_type="STEP_2")
    obs.record_event(workflow_id=wf_b, agent_id="AgentB", event_type="STEP_1")

    events_a = obs.get_events(workflow_id=wf_a)
    events_b = obs.get_events(workflow_id=wf_b)

    assert len(events_a) == 2
    assert all(e["workflow_id"] == wf_a for e in events_a)

    assert len(events_b) == 1
    assert events_b[0]["workflow_id"] == wf_b


# -----------------------------------------------------------------------------
# Test 4: Records failures and retry attempts
# -----------------------------------------------------------------------------
def test_records_failures_and_retry_attempts(obs):
    wf_id = str(uuid4())

    # 1. Recoverable retry attempt
    obs.record_failure_or_retry(
        workflow_id=wf_id,
        agent_id="RecoveryAgent",
        error_message="Hospital B capacity exhausted",
        classification="INSUFFICIENT_CAPACITY",
        attempt=2,
        max_attempts=3,
        is_recoverable=True,
    )

    retry_events = obs.get_events(workflow_id=wf_id, event_type="RETRY_ATTEMPT")
    assert len(retry_events) == 1
    p1 = retry_events[0]["payload"]
    assert p1["attempt"] == 2
    assert p1["classification"] == "INSUFFICIENT_CAPACITY"
    assert p1["is_recoverable"] is True
    assert retry_events[0]["status"] == "RETRYING"

    # 2. Fatal unrecoverable failure
    obs.record_failure_or_retry(
        workflow_id=wf_id,
        agent_id="VerificationAgent",
        error_message="Provider certificate revoked",
        classification="SECURITY_POLICY_VIOLATION",
        attempt=1,
        max_attempts=1,
        is_recoverable=False,
    )

    fatal_events = obs.get_events(workflow_id=wf_id, event_type="FAILURE")
    assert len(fatal_events) == 1
    assert fatal_events[0]["status"] == "FAILED"


# -----------------------------------------------------------------------------
# Test 5: Tracks provider-selection decisions
# -----------------------------------------------------------------------------
def test_tracks_provider_selection_decisions(obs):
    wf_id = str(uuid4())

    obs.record_decision(
        workflow_id=wf_id,
        agent_id="NegotiationAgent",
        decision_type="OFFER_SELECTION",
        selected_option="lab-c",
        rationale="Offered full quantity (3 slots) with lowest delivery delay (2 hours)",
        evaluated_candidates=["hospital-b", "lab-c"],
        criteria={"urgency": "URGENT", "price_weight": 0.3, "speed_weight": 0.7},
    )

    events = obs.get_events(workflow_id=wf_id, event_type="OFFER_SELECTION")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["selected_option"] == "lab-c"
    assert "lowest delivery delay" in payload["rationale"]
    assert payload["evaluated_candidates"] == ["hospital-b", "lab-c"]


# -----------------------------------------------------------------------------
# Test 6: Records approval and verification events
# -----------------------------------------------------------------------------
def test_records_approval_and_verification_events(obs):
    wf_id = str(uuid4())

    # Verification outcome
    obs.record_verification(
        workflow_id=wf_id,
        provider_id="lab-c",
        status="VERIFIED",
        passed_checks=["registry_exists", "wallet_match", "authoritative_capacity_available"],
        failed_checks=[],
        details="All credentials accredited and capacity validated",
    )

    ver_events = obs.get_events(workflow_id=wf_id, event_type="VERIFICATION_OUTCOME")
    assert len(ver_events) == 1
    assert ver_events[0]["status"] == "SUCCESS"
    assert ver_events[0]["payload"]["provider_id"] == "lab-c"
    assert len(ver_events[0]["payload"]["passed_checks"]) == 3

    # Institutional human approval
    obs.record_approval(
        workflow_id=wf_id,
        approved_by="Dr. Alice Vance (Chief Medical Officer)",
        checkpoint_name="HUMAN_APPROVAL",
        details="Authorized clinical emergency transfer",
    )

    app_events = obs.get_events(workflow_id=wf_id, event_type="APPROVAL_CHECKPOINT")
    assert len(app_events) == 1
    assert app_events[0]["payload"]["approved_by"] == "Dr. Alice Vance (Chief Medical Officer)"


# -----------------------------------------------------------------------------
# Test 7: Tracks blockchain transaction events
# -----------------------------------------------------------------------------
def test_tracks_blockchain_transaction_events(obs):
    wf_id = str(uuid4())
    tx = "0x4a9b6c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b"

    obs.record_blockchain_transaction(
        workflow_id=wf_id,
        tx_hash=tx,
        operation="LOCK_AND_COMMIT",
        status="CONFIRMED",
        block_number=1286005,
        gas_used=89400,
        details="2PC capacity lock and commitment finalized on Coral Testnet",
    )

    events = obs.get_events(workflow_id=wf_id, event_type="BLOCKCHAIN_TRANSACTION")
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["tx_hash"] == tx
    assert p["operation"] == "LOCK_AND_COMMIT"
    assert p["block_number"] == 1286005
    assert events[0]["status"] == "CONFIRMED"


# -----------------------------------------------------------------------------
# Test 8: Generates workflow summaries
# -----------------------------------------------------------------------------
def test_generates_workflow_summaries(obs):
    wf_id = str(uuid4())

    # Timeline of lifecycle events
    obs.record_event(workflow_id=wf_id, agent_id="RequestAgent", event_type="AGENT_COMPLETE", status="SUCCESS")
    obs.record_decision(
        workflow_id=wf_id,
        agent_id="NegotiationAgent",
        decision_type="OFFER_SELECTION",
        selected_option="lab-c",
        rationale="Lowest turnaround time",
    )
    obs.record_verification(workflow_id=wf_id, provider_id="lab-c", status="VERIFIED")
    obs.record_approval(workflow_id=wf_id, approved_by="Dr. Vance")
    obs.record_blockchain_transaction(
        workflow_id=wf_id,
        tx_hash="0x1234abcd",
        operation="LOCK_AND_COMMIT",
        status="CONFIRMED",
        block_number=1000,
    )

    summary = obs.get_workflow_summary(wf_id)
    assert summary["workflow_id"] == wf_id
    assert summary["status"] == "CONFIRMED"
    assert summary["total_events"] == 5
    assert "RequestAgent" in summary["agents_involved"]
    assert "NegotiationAgent" in summary["agents_involved"]
    assert len(summary["decisions"]) == 1
    assert summary["decisions"][0]["selected"] == "lab-c"
    assert len(summary["checkpoints"]) == 2  # Verification + Approval
    assert len(summary["blockchain_transactions"]) == 1
    assert len(summary["timeline"]) == 5


# -----------------------------------------------------------------------------
# Test 9: Redacts secrets and PII
# -----------------------------------------------------------------------------
def test_redacts_secrets_and_pii(obs):
    wf_id = str(uuid4())

    dirty_payload = {
        "provider_id": "lab-c",
        # Credentials to redact
        "private_key": "0x4a9b6c8d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b",
        "api_key": "secret_api_key_12345",
        "token": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        # Patient PII to redact
        "patient_name": "Jane Doe",
        "ssn": "123-45-6789",
        "mrn": "MRN-987654",
        "diagnosis": "Trauma ICU admission",
        # Embedded strings with secrets or PII
        "debug_log": "Connected with private_key=0xabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdef and patient SSN 999-88-7777",
    }

    obs.record_event(
        workflow_id=wf_id,
        agent_id="ProviderAgent",
        event_type="DISPATCH",
        payload=dirty_payload,
    )

    events = obs.get_events(workflow_id=wf_id)
    assert len(events) == 1
    clean = events[0]["payload"]

    # Invariants: Zero secrets & Zero PII in observability storage
    assert clean["private_key"] == REDACTED_SECRET
    assert clean["api_key"] == REDACTED_SECRET
    assert clean["patient_name"] == REDACTED_PII
    assert clean["ssn"] == REDACTED_PII
    assert clean["mrn"] == REDACTED_PII
    assert clean["diagnosis"] == REDACTED_PII
    assert "0x4a9b6c8d" not in json.dumps(clean)
    assert "123-45-6789" not in json.dumps(clean)
    assert "Jane Doe" not in json.dumps(clean)
    assert "999-88-7777" not in json.dumps(clean)
    assert REDACTED_PII in clean["debug_log"]


# -----------------------------------------------------------------------------
# Test 10: Handles observability-storage failures without corrupting workflow execution
# -----------------------------------------------------------------------------
def test_handles_observability_storage_failures_without_corrupting_workflow(obs, monkeypatch):
    wf_id = str(uuid4())

    def broken_save(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated disk full or database locked")

    monkeypatch.setattr("app.services.agent_observability.save_observability_event", broken_save)

    # 1. Direct record call does not crash
    result = obs.record_event(workflow_id=wf_id, agent_id="DiscoveryAgent", event_type="STEP")
    assert result is not None

    # 2. Context manager does not crash the calling workflow
    execution_completed = False
    with obs.track_agent(workflow_id=wf_id, agent_id="NegotiationAgent"):
        execution_completed = True

    assert execution_completed is True
