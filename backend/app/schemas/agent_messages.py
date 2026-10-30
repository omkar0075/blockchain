from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, field_validator, ConfigDict


class AgentStatus(str, Enum):
    """Tracks the execution state of an agent or task within a MEDway workflow."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"


class AgentError(BaseModel):
    """Standard format for reporting agent failures across the coordination network."""
    model_config = ConfigDict(extra="ignore")

    error_code: str = Field(..., min_length=1, description="Machine-readable error category or code")
    message: str = Field(..., min_length=1, description="Human-readable explanation of failure")
    agent_name: str = Field(..., min_length=1, description="Agent reporting the error")
    workflow_id: UUID = Field(..., description="ID of the associated workflow")
    recoverable: bool = Field(default=False, description="Whether the workflow can safely retry or recover")

    @field_validator("error_code", "message", "agent_name")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Field cannot be empty or whitespace only")
        return stripped


class AgentMessage(BaseModel):
    """Standard message exchanged between autonomous agents."""
    model_config = ConfigDict(extra="ignore")

    message_id: UUID = Field(default_factory=uuid4, description="Unique message identifier")
    workflow_id: UUID = Field(..., description="Identifies the overall multi-agent workflow")
    sender: str = Field(..., min_length=1, description="Agent sending the message")
    receiver: str = Field(..., min_length=1, description="Agent receiving the message")
    message_type: str = Field(..., min_length=1, description="Type/verb of communication")
    payload: dict[str, Any] = Field(default_factory=dict, description="Actual structured message data")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Message creation time (UTC, timezone-aware)",
    )

    @field_validator("sender", "receiver", "message_type")
    @classmethod
    def validate_identifiers(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Identifier cannot be empty or whitespace only")
        return stripped

    @field_validator("timestamp")
    @classmethod
    def validate_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("Timestamp must be timezone-aware (UTC)")
        return v


class AgentResult(BaseModel):
    """Standard output returned by an agent upon task execution."""
    model_config = ConfigDict(extra="ignore")

    agent_name: str = Field(..., min_length=1, description="Name of the reporting agent")
    workflow_id: UUID = Field(..., description="Workflow identifier")
    status: AgentStatus = Field(..., description="Current agent execution status")
    data: dict[str, Any] = Field(default_factory=dict, description="Output payload produced by the agent")
    execution_time: float = Field(
        ...,
        ge=0.0,
        description="Task execution duration in seconds (must be non-negative)",
    )
    error: Optional[AgentError] = Field(default=None, description="Optional error details if execution failed")

    @field_validator("agent_name")
    @classmethod
    def validate_agent_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Agent name cannot be empty or whitespace only")
        return stripped

