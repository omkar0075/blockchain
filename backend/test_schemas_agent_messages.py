import json
from datetime import datetime, timezone
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from app.schemas.agent_messages import (
    AgentStatus,
    AgentMessage,
    AgentResult,
    AgentError,
)


def test_agent_status_values():
    assert AgentStatus.PENDING == "PENDING"
    assert AgentStatus.RUNNING == "RUNNING"
    assert AgentStatus.COMPLETED == "COMPLETED"
    assert AgentStatus.FAILED == "FAILED"
    assert AgentStatus.WAITING_FOR_APPROVAL == "WAITING_FOR_APPROVAL"

    # Invalid status should fail validation
    with pytest.raises(ValidationError):
        AgentResult(
            agent_name="DiscoveryAgent",
            workflow_id=uuid4(),
            status="INVALID_STATUS", # type: ignore
            execution_time=0.5,
        )


def test_valid_agent_message_creation():
    wf_id = uuid4()
    msg = AgentMessage(
        workflow_id=wf_id,
        sender="RequestAgent",
        receiver="DiscoveryAgent",
        message_type="DISCOVERY_TASK",
        payload={"resource_type": "diagnostic_slot", "quantity": 2},
    )

    assert isinstance(msg.message_id, UUID)
    assert msg.workflow_id == wf_id
    assert msg.sender == "RequestAgent"
    assert msg.receiver == "DiscoveryAgent"
    assert msg.message_type == "DISCOVERY_TASK"
    assert msg.payload["quantity"] == 2
    assert msg.timestamp.tzinfo is not None
    assert msg.timestamp.tzinfo == timezone.utc or msg.timestamp.utcoffset() is not None


def test_timezone_aware_timestamp_enforcement():
    # Naive timestamp should be rejected
    naive_dt = datetime.now()
    with pytest.raises(ValidationError) as exc:
        AgentMessage(
            workflow_id=uuid4(),
            sender="RequestAgent",
            receiver="DiscoveryAgent",
            message_type="TASK",
            timestamp=naive_dt,
        )
    assert "Timestamp must be timezone-aware" in str(exc.value)


def test_invalid_identifiers_rejected():
    wf_id = uuid4()
    # Empty string
    with pytest.raises(ValidationError):
        AgentMessage(
            workflow_id=wf_id,
            sender="",
            receiver="DiscoveryAgent",
            message_type="TASK",
        )

    # Whitespace only
    with pytest.raises(ValidationError):
        AgentMessage(
            workflow_id=wf_id,
            sender="   ",
            receiver="DiscoveryAgent",
            message_type="TASK",
        )

    with pytest.raises(ValidationError):
        AgentMessage(
            workflow_id=wf_id,
            sender="RequestAgent",
            receiver="",
            message_type="TASK",
        )


def test_agent_result_with_and_without_error():
    wf_id = uuid4()
    
    # Success without error
    res_ok = AgentResult(
        agent_name="DiscoveryAgent",
        workflow_id=wf_id,
        status=AgentStatus.COMPLETED,
        data={"offers_found": 3},
        execution_time=0.142,
    )
    assert res_ok.error is None
    assert res_ok.status == AgentStatus.COMPLETED
    assert res_ok.execution_time == 0.142

    # Negative execution time should be rejected
    with pytest.raises(ValidationError):
        AgentResult(
            agent_name="DiscoveryAgent",
            workflow_id=wf_id,
            status=AgentStatus.COMPLETED,
            execution_time=-0.5,
        )

    # Failure with AgentError
    err = AgentError(
        error_code="CAPACITY_EXHAUSTED",
        message="No accredited provider currently has available diagnostic slots",
        agent_name="DiscoveryAgent",
        workflow_id=wf_id,
        recoverable=True,
    )
    res_fail = AgentResult(
        agent_name="DiscoveryAgent",
        workflow_id=wf_id,
        status=AgentStatus.FAILED,
        data={},
        execution_time=0.088,
        error=err,
    )
    assert res_fail.error is not None
    assert res_fail.error.error_code == "CAPACITY_EXHAUSTED"
    assert res_fail.error.recoverable is True


def test_json_serialization_and_deserialization():
    wf_id = uuid4()
    msg = AgentMessage(
        workflow_id=wf_id,
        sender="NegotiationAgent",
        receiver="BlockchainAgent",
        message_type="LOCK_CAPACITY_REQUEST",
        payload={"provider_id": "lab-c", "slots": 2, "duration": 1800},
    )

    # Serialize to JSON string
    json_str = msg.model_dump_json()
    assert isinstance(json_str, str)
    
    # Parse back
    raw_dict = json.loads(json_str)
    assert raw_dict["sender"] == "NegotiationAgent"
    assert raw_dict["payload"]["slots"] == 2
    assert str(wf_id) == raw_dict["workflow_id"]

    # Reconstitute into model
    msg_reconstituted = AgentMessage.model_validate_json(json_str)
    assert msg_reconstituted.workflow_id == wf_id
    assert msg_reconstituted.message_id == msg.message_id
    assert msg_reconstituted.timestamp == msg.timestamp


if __name__ == "__main__":
    test_agent_status_values()
    test_valid_agent_message_creation()
    test_timezone_aware_timestamp_enforcement()
    test_invalid_identifiers_rejected()
    test_agent_result_with_and_without_error()
    test_json_serialization_and_deserialization()
    print("ALL AGENT MESSAGE SCHEMA TESTS PASSED SUCCESSFULLY!")
