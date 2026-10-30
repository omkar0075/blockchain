from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import pytest

from app.schemas.resource_request import LocationArea, ResourceRequest, ResourceType
from app.agents.context import AgentContext


def create_sample_request() -> ResourceRequest:
    now = datetime.now(timezone.utc)
    return ResourceRequest(
        requester_institution_id="hospital-a",
        resource_type=ResourceType.ICU_BED,
        quantity=2,
        location=LocationArea(city="Metropolis"),
        required_by=now + timedelta(hours=8),
    )


def test_valid_agent_context_creation():
    """Verify standard creation of an AgentContext instance."""
    wf_id = uuid4()
    req = create_sample_request()

    ctx = AgentContext(
        workflow_id=wf_id,
        request=req,
        current_agent="RequestAgent",
        metadata={"priority_routed": True},
    )

    assert isinstance(ctx.context_id, UUID)
    assert ctx.workflow_id == wf_id
    assert ctx.request.resource_type == ResourceType.ICU_BED
    assert ctx.current_agent == "RequestAgent"
    assert ctx.approval_required is True
    assert ctx.approved_by is None
    assert ctx.approved_at is None
    assert ctx.created_at.tzinfo == timezone.utc
    assert ctx.metadata["priority_routed"] is True


def test_agent_transition():
    """Verify switching current_agent across the multi-agent pipeline."""
    ctx = AgentContext(
        workflow_id=uuid4(),
        request=create_sample_request(),
        current_agent="DiscoveryAgent",
    )
    assert ctx.current_agent == "DiscoveryAgent"

    ctx.update_agent("NegotiationAgent")
    assert ctx.current_agent == "NegotiationAgent"

    with pytest.raises(ValueError, match="agent_name cannot be empty or whitespace only"):
        ctx.update_agent("   ")


def test_record_approval():
    """Verify recording attributable institutional approval."""
    now = datetime.now(timezone.utc)
    ctx = AgentContext(
        workflow_id=uuid4(),
        request=create_sample_request(),
        current_agent="VerificationAgent",
        created_at=now,
    )
    assert ctx.approval_required is True

    approval_time = now + timedelta(minutes=5)
    ctx.record_approval(approved_by="Dr. Sarah Connor (Chief Medical Officer)", approved_at=approval_time)

    assert ctx.approval_required is False
    assert ctx.approved_by == "Dr. Sarah Connor (Chief Medical Officer)"
    assert ctx.approved_at == approval_time


def test_approval_inconsistency_rejected():
    """Verify attributable approval invariants."""
    now = datetime.now(timezone.utc)
    req = create_sample_request()

    # 1. approved_by provided without approved_at
    with pytest.raises(ValueError, match="approved_at timestamp must be provided when approved_by is set"):
        AgentContext(
            workflow_id=uuid4(),
            request=req,
            current_agent="BlockchainAgent",
            approved_by="Admin Officer",
            approved_at=None,
        )

    # 2. approved_at provided without approved_by
    with pytest.raises(ValueError, match="approved_by must be specified when approved_at is provided"):
        AgentContext(
            workflow_id=uuid4(),
            request=req,
            current_agent="BlockchainAgent",
            approved_by=None,
            approved_at=now,
        )

    # 3. approved_at is in the past relative to created_at
    with pytest.raises(ValueError, match="approved_at cannot be earlier than created_at"):
        AgentContext(
            workflow_id=uuid4(),
            request=req,
            current_agent="BlockchainAgent",
            created_at=now,
            approved_by="Officer",
            approved_at=now - timedelta(hours=1),
        )

    # 4. record_approval with empty name
    ctx = AgentContext(
        workflow_id=uuid4(),
        request=req,
        current_agent="BlockchainAgent",
    )
    with pytest.raises(ValueError, match="approved_by cannot be empty or whitespace only"):
        ctx.record_approval(approved_by="  ")


def test_prohibit_secrets():
    """Security check: reject private keys, secrets, mnemonics in context and metadata."""
    forbidden_keys = [
        "private_key",
        "secret",
        "credential",
        "mnemonic",
        "password",
        "api_key",
    ]

    for key in forbidden_keys:
        # Check top-level
        with pytest.raises(ValueError, match="Security violation: forbidden keys detected"):
            AgentContext.model_validate({
                "workflow_id": str(uuid4()),
                "request": create_sample_request().model_dump(),
                "current_agent": "RequestAgent",
                key: "sensitive_data_value",
            })

        # Check in metadata
        with pytest.raises(ValueError, match="Security violation: forbidden keys detected in metadata"):
            AgentContext(
                workflow_id=uuid4(),
                request=create_sample_request(),
                current_agent="RequestAgent",
                metadata={key: "sensitive_data_value"},
            )


def test_prohibit_pii():
    """Security check: reject patient identifiers in context and metadata."""
    forbidden_pii = [
        "patient_name",
        "patient_id",
        "ssn",
        "mrn",
        "aadhaar",
    ]

    for pii in forbidden_pii:
        with pytest.raises(ValueError, match="Security violation: forbidden keys detected"):
            AgentContext.model_validate({
                "workflow_id": str(uuid4()),
                "request": create_sample_request().model_dump(),
                "current_agent": "RequestAgent",
                pii: "Alice",
            })

        with pytest.raises(ValueError, match="Security violation: forbidden keys detected in metadata"):
            AgentContext(
                workflow_id=uuid4(),
                request=create_sample_request(),
                current_agent="RequestAgent",
                metadata={pii: "Alice"},
            )


def test_json_roundtrip_serialization():
    """Verify JSON serialization roundtrip."""
    now = datetime.now(timezone.utc)
    wf_id = uuid4()
    req = create_sample_request()

    ctx = AgentContext(
        workflow_id=wf_id,
        request=req,
        current_agent="RecoveryAgent",
        created_at=now,
        approved_by="Dr. Director",
        approved_at=now + timedelta(minutes=2),
        approval_required=False,
    )

    json_str = ctx.model_dump_json()
    assert "RecoveryAgent" in json_str
    assert "Dr. Director" in json_str

    restored = AgentContext.model_validate_json(json_str)
    assert restored.context_id == ctx.context_id
    assert restored.workflow_id == wf_id
    assert restored.current_agent == "RecoveryAgent"
    assert restored.approved_by == "Dr. Director"
    assert restored.approval_required is False
    assert restored.created_at.tzinfo == timezone.utc

