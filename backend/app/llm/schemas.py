from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field


# =============================================================================
# Custom LLM Exceptions
# =============================================================================

class LLMError(Exception):
    """Base exception for LLM operations in MEDway."""
    pass


class LLMTimeoutError(LLMError):
    """Raised when an LLM provider request exceeds the timeout threshold."""
    pass


class LLMProviderError(LLMError):
    """Raised when the underlying LLM API returns an error or status failure."""
    pass


class LLMOutputValidationError(LLMError):
    """Raised when LLM output cannot be parsed into the expected structured model."""
    pass


class LLMGuardrailViolation(LLMError):
    """Raised when input or output violates security, privacy, or size guardrails."""
    pass


# =============================================================================
# Telemetry and Execution Models
# =============================================================================

class LLMResponse(BaseModel):
    """Raw output and metadata from an LLM generation call."""
    model_config = ConfigDict(extra="ignore")

    raw_text: str = Field(..., description="Raw text returned by model")
    parsed: Optional[Any] = Field(default=None, description="Parsed structured object if schema was provided")
    model: str = Field(default="mock-model", description="Model identifier used")
    prompt_tokens: Optional[int] = Field(default=None, description="Input token count")
    completion_tokens: Optional[int] = Field(default=None, description="Output token count")
    duration_ms: float = Field(default=0.0, description="Round-trip latency in milliseconds")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# =============================================================================
# Controlled Reasoning Output Models (Strictly extra='forbid')
# =============================================================================

class StructuredReasoning(BaseModel):
    """Generic structured thinking model for auditable decision support."""
    model_config = ConfigDict(extra="forbid")

    thinking: str = Field(..., description="Internal step-by-step reasoning steps")
    decision: str = Field(..., description="Synthesized recommendation or decision")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    rationale: list[str] = Field(default_factory=list, description="Explicit bulleted justifications")


class RequestInterpretationOutput(BaseModel):
    """Structured output for natural language healthcare request interpretation."""
    model_config = ConfigDict(extra="forbid")

    resource_type: Optional[str] = Field(default=None, description="Extracted resource category")
    quantity: Optional[int] = Field(default=None, ge=1, description="Extracted resource quantity")
    urgency: Optional[str] = Field(default=None, description="Extracted urgency level")
    location: Optional[str] = Field(default=None, description="Extracted city, postal code, or area")
    required_by_hours: Optional[int] = Field(default=None, ge=1, description="Delivery deadline in hours")
    is_ambiguous: bool = Field(default=False, description="Whether the request is missing essential information")
    missing_fields: list[str] = Field(default_factory=list, description="List of required parameters that were omitted")
    clarification_prompt: Optional[str] = Field(default=None, description="Clarification question to present to the user")


class OfferEvaluation(BaseModel):
    """Detailed evaluation of an individual provider offer."""
    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(..., description="Provider identifier")
    strengths: list[str] = Field(default_factory=list, description="Positive aspects (speed, volume, price)")
    weaknesses: list[str] = Field(default_factory=list, description="Drawbacks or constraints (cost, partial quantity)")
    meets_hard_constraints: bool = Field(default=True, description="Whether this offer meets budget and deadline limits")


class NegotiationAdviceOutput(BaseModel):
    """Structured advice model for provider offer evaluation and counteroffers."""
    model_config = ConfigDict(extra="forbid")

    recommended_action: str = Field(..., description="'RECOMMEND_ACCEPTANCE', 'SUGGEST_COUNTEROFFER', or 'REJECT_ALL'")
    recommended_provider_id: Optional[str] = Field(default=None, description="Preferred provider identifier")
    counteroffer_quantity: Optional[int] = Field(default=None, description="Suggested counteroffer quantity")
    evaluations: list[OfferEvaluation] = Field(default_factory=list, description="Per-offer strengths and weaknesses")
    trade_offs: list[str] = Field(default_factory=list, description="Analysis of trade-offs across speed, price, and volume")
    explanation: str = Field(..., description="Human-readable explanation of the recommendation")


class RecoveryAdviceOutput(BaseModel):
    """Structured guidance for workflow failures and alternative discovery."""
    model_config = ConfigDict(extra="forbid")

    failure_classification: str = Field(..., description="Classified root cause")
    root_cause_explanation: str = Field(..., description="Clear explanation of why the coordination step failed")
    suggested_recovery_path: str = Field(..., description="Recommended remediation strategy")
    is_recoverable: bool = Field(default=True, description="Whether automatic recovery should be attempted")
    requires_human_intervention: bool = Field(default=False, description="Whether human review or approval is necessary")
    alternative_provider_ids: list[str] = Field(default_factory=list, description="Candidate providers to evaluate")
    suggested_counteroffer_quantity: Optional[int] = Field(default=None, description="Suggested renegotiation quantity if partial capacity is viable")
    remediation_steps: list[str] = Field(default_factory=list, description="Explicit sequential steps to remediate the failure")

