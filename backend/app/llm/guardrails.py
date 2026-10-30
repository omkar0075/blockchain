import json
import logging
import re
from typing import Any, Optional, Union
from pydantic import BaseModel, ValidationError

from .schemas import LLMGuardrailViolation, LLMOutputValidationError

logger = logging.getLogger("medway.llm.guardrails")

# Regular expressions for prompt injection defense
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|rules|prompts|constraints)"),
    re.compile(r"(?i)system\s+override"),
    re.compile(r"(?i)disregard\s+(?:all\s+)?(?:prior|above)\s+(?:constraints|instructions|rules)"),
    re.compile(r"(?i)you\s+are\s+now\s+(?:unrestricted|in\s+developer\s+mode|dan|jailbroken)"),
    re.compile(r"(?i)bypass\s+(?:verification|security|guardrails|safety)"),
    re.compile(r"(?i)pretend\s+you\s+have\s+(?:root|admin|executive|signing)\s+authority"),
]

# Sensitive credentials that must never be processed or logged
CREDENTIAL_PATTERNS = [
    re.compile(r"\b0x[a-fA-F0-9]{64}\b"),  # 32-byte hex private key / secret
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]+"),
    re.compile(r"(?i)(private_key|secret_key|api_key|password)\s*[:=]\s*\S+"),
]

# Prohibited authority fields that the LLM is strictly forbidden from setting
FORBIDDEN_AUTHORITY_FIELDS = {
    "bypass_verification",
    "auto_approve",
    "execute_transaction",
    "sign_transaction",
    "capacity_override",
    "private_key",
    "force_commit",
}


def validate_input(prompt: str, max_chars: int = 4000) -> str:
    """
    Validate user/agent input before passing it to an LLM provider.
    Enforces size boundaries, rejects prompt injections, and scans for credentials.
    """
    if not prompt or not prompt.strip():
        raise LLMGuardrailViolation("LLM prompt cannot be empty or whitespace only")

    if len(prompt) > max_chars:
        raise LLMGuardrailViolation(
            f"Input size exceeds safety limit ({len(prompt)} characters > {max_chars} max)"
        )

    # 1. Prompt Injection Detection
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(prompt):
            logger.warning("Prompt injection pattern detected: %s", pattern.pattern)
            raise LLMGuardrailViolation(
                "Security violation: Prompt injection attempt detected overriding system instructions"
            )

    # 2. Scanning for exposed private keys or credentials
    for cred_pat in CREDENTIAL_PATTERNS:
        if cred_pat.search(prompt):
            raise LLMGuardrailViolation(
                "Security violation: Prohibited credential or private key detected in input prompt"
            )

    return prompt


def extract_json_from_text(raw_text: str) -> str:
    """Extract JSON content from potential Markdown fences or prose formatting."""
    cleaned = raw_text.strip()

    # Match ```json ... ``` blocks
    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fenced_match:
        return fenced_match.group(1).strip()

    # Find the outermost { ... }
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return cleaned[first_brace : last_brace + 1].strip()

    return cleaned


def validate_output(
    raw_output: str,
    schema: Optional[type[BaseModel]] = None,
    max_chars: int = 8000,
) -> Union[dict[str, Any], BaseModel]:
    """
    Validate and parse raw LLM output into validated structured responses.
    Guarantees:
    - Enforces strict response size limits.
    - Rejects malformed JSON.
    - Rejects unsupported output fields (when schema configured with extra='forbid').
    - Strictly blocks unauthorized authority escalation fields (e.g. bypass_verification).
    """
    if not raw_output or not raw_output.strip():
        raise LLMOutputValidationError("LLM returned empty or whitespace-only response")

    if len(raw_output) > max_chars:
        raise LLMGuardrailViolation(
            f"Output size exceeds safety limit ({len(raw_output)} characters > {max_chars} max)"
        )

    json_str = extract_json_from_text(raw_output)

    try:
        parsed_data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from LLM output: %s", exc)
        raise LLMOutputValidationError(f"Malformed JSON returned by LLM: {exc}") from exc

    if not isinstance(parsed_data, dict):
        raise LLMOutputValidationError("LLM response JSON must be a structured object (dict)")

    # Anti-Authority Invariant: The LLM cannot authorize, commit, or bypass verification
    for forbidden_field in FORBIDDEN_AUTHORITY_FIELDS:
        if forbidden_field in parsed_data and parsed_data[forbidden_field]:
            raise LLMGuardrailViolation(
                f"Authority violation: LLM attempted to execute unauthorized action '{forbidden_field}'"
            )

    # Validate against target Pydantic schema
    if schema is not None:
        try:
            return schema.model_validate(parsed_data)
        except ValidationError as val_err:
            logger.error("Schema validation failure on LLM output: %s", val_err)
            raise LLMOutputValidationError(
                f"LLM output violates required schema structure: {val_err}"
            ) from val_err

    return parsed_data


def sanitize_log_message(msg: str, api_keys: Optional[list[str]] = None) -> str:
    """Mask credentials, API keys, and private keys in log messages."""
    sanitized = str(msg)

    # Mask known API keys
    if api_keys:
        for k in api_keys:
            if k and len(k) > 4:
                sanitized = sanitized.replace(k, "[REDACTED_API_KEY]")

    # Mask general private keys and bearer tokens
    for pattern in CREDENTIAL_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)

    return sanitized
