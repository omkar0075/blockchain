from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict

from ..schemas.resource_request import FORBIDDEN_PII_KEYWORDS, ResourceRequest

FORBIDDEN_SECURITY_KEYWORDS = {
    *FORBIDDEN_PII_KEYWORDS,
    "private_key",
    "secret",
    "credential",
    "mnemonic",
    "password",
    "api_key",
    "auth_token",
    "bearer_token",
}


class AgentContext(BaseModel):
    """
    Shared workflow execution context coordinating multi-agent actions.
    
    Security & Architectural Invariants:
    1. Zero Secrets & PII: Rejects private keys, credentials, mnemonics, and patient PII.
    2. Non-Authoritative on Capacity: Context carries operational parameters, but
       never serves as the source of truth for live provider capacity (which is on-chain).
    3. Attributable Approvals: Approval requires explicit approved_by attribution
       and a timezone-aware timestamp.
    4. Institutional Boundary: Context state cannot override institutional access permissions.
    """
    model_config = ConfigDict(extra="ignore")

    context_id: UUID = Field(
        default_factory=uuid4,
        description="Unique identifier for this workflow execution context instance",
    )
    workflow_id: UUID = Field(
        ...,
        description="Identifies the overall multi-agent workflow",
    )
    request: ResourceRequest = Field(
        ...,
        description="Validated structured healthcare resource request",
    )
    current_agent: str = Field(
        ...,
        min_length=1,
        description="Identifier of the currently executing agent (e.g. 'DiscoveryAgent')",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Context creation timestamp (UTC, timezone-aware)",
    )
    approval_required: bool = Field(
        default=True,
        description="Whether human institutional approval is required before commitment",
    )
    approved_by: Optional[str] = Field(
        default=None,
        description="Attributable user or institutional officer who approved the allocation",
    )
    approved_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when institutional authorization was granted (UTC, timezone-aware)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Safe operational coordination metadata (audited for secrets and PII)",
    )

    @field_validator("current_agent")
    @classmethod
    def validate_agent_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("current_agent cannot be empty or whitespace only")
        return stripped

    @field_validator("created_at", "approved_at")
    @classmethod
    def validate_timezones(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
                raise ValueError("Timestamps must be timezone-aware (UTC)")
        return v

    @model_validator(mode="before")
    @classmethod
    def reject_secrets_and_pii(cls, values: Any) -> Any:
        """Scan top-level and nested metadata keys for prohibited credentials and PII."""
        if isinstance(values, dict):
            # Check top-level keys
            top_level_keys = {k.lower() for k in values.keys()}
            violating_keys = top_level_keys.intersection(FORBIDDEN_SECURITY_KEYWORDS)
            if violating_keys:
                raise ValueError(
                    f"Security violation: forbidden keys detected in context ({', '.join(sorted(violating_keys))})"
                )

            # Check metadata keys
            meta = values.get("metadata")
            if isinstance(meta, dict):
                meta_keys = {str(k).lower() for k in meta.keys()}
                meta_violations = meta_keys.intersection(FORBIDDEN_SECURITY_KEYWORDS)
                if meta_violations:
                    raise ValueError(
                        f"Security violation: forbidden keys detected in metadata ({', '.join(sorted(meta_violations))})"
                    )

        return values

    @model_validator(mode="after")
    def validate_approval_consistency(self) -> "AgentContext":
        """Ensures attributable authorization consistency."""
        if self.approved_by is not None:
            stripped_approver = self.approved_by.strip()
            if not stripped_approver:
                raise ValueError("approved_by cannot be empty or whitespace only")
            if self.approved_at is None:
                raise ValueError("approved_at timestamp must be provided when approved_by is set")
            if self.approved_at < self.created_at:
                raise ValueError("approved_at cannot be earlier than created_at")
        elif self.approved_at is not None:
            raise ValueError("approved_by must be specified when approved_at is provided")

        return self

    def update_agent(self, agent_name: str) -> None:
        """Transition execution context to the next agent in the coordination pipeline."""
        stripped = agent_name.strip() if agent_name else ""
        if not stripped:
            raise ValueError("agent_name cannot be empty or whitespace only")
        self.current_agent = stripped

    def record_approval(self, approved_by: str, approved_at: Optional[datetime] = None) -> None:
        """
        Record explicit institutional human authorization.
        
        Requires explicit attribution and sets the authorization timestamp.
        """
        stripped_approver = approved_by.strip() if approved_by else ""
        if not stripped_approver:
            raise ValueError("approved_by cannot be empty or whitespace only")

        effective_time = approved_at or datetime.now(timezone.utc)
        if effective_time.tzinfo is None or effective_time.tzinfo.utcoffset(effective_time) is None:
            raise ValueError("approved_at timestamp must be timezone-aware (UTC)")
        if effective_time < self.created_at:
            raise ValueError("approved_at cannot be earlier than created_at")

        self.approved_by = stripped_approver
        self.approved_at = effective_time
        self.approval_required = False

