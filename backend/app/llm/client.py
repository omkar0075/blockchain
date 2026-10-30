import asyncio
import logging
from typing import Any, Optional, TypeVar
from pydantic import BaseModel

from .guardrails import sanitize_log_message, validate_input, validate_output
from .provider import LLMProvider, MockLLMProvider, get_llm_provider
from .schemas import (
    LLMError,
    LLMGuardrailViolation,
    LLMOutputValidationError,
    LLMProviderError,
    LLMResponse,
    LLMTimeoutError,
)

logger = logging.getLogger("medway.llm.client")

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """
    Controlled LLM Reasoning Client for MEDway.

    Orchestrates communication with model providers, strictly enforcing:
    1. Input/Output Guardrails: Prompt injection defenses, length limits, and anti-authority invariants.
    2. Zero Sensitive Leaks: Private keys, credentials, and API keys are strictly masked in logs.
    3. Structured Outputs: Guarantees responses adhere to typed Pydantic models with strict field validation.
    4. Bounded Resilience: Automatic exponential backoff on transient timeouts or network faults.
    5. Advisory Role Only: The LLM may recommend, interpret and explain. It CANNOT authorize reservations,
       change capacity, sign transactions, access private keys, or bypass verification.
    """

    def __init__(
        self,
        provider: Optional[LLMProvider] = None,
        default_model: str = "mock-deterministic",
        temperature: float = 0.0,
        timeout: float = 15.0,
        max_retries: int = 2,
        backoff_factor: float = 0.1,
    ) -> None:
        self.provider = provider or get_llm_provider()
        self.default_model = default_model
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    async def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 1024,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> T:
        """
        Generate and validate a structured response matching target Pydantic schema T.
        Applies input guardrails, attempts execution with backoff, and validates output.
        """
        # 1. Input Guardrails & Injection Defense
        validated_prompt = validate_input(prompt)
        validated_sys_prompt = validate_input(system_prompt) if system_prompt else None

        temp = temperature if temperature is not None else self.temperature
        to = timeout if timeout is not None else self.timeout
        max_attempts = (retries if retries is not None else self.max_retries) + 1

        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                # 2. Model Invocation
                response: LLMResponse = await self.provider.generate(
                    prompt=validated_prompt,
                    system_prompt=validated_sys_prompt,
                    response_schema=schema,
                    temperature=temp,
                    max_tokens=max_tokens,
                    timeout=to,
                )

                # 3. Output Guardrails & Schema Parsing
                validated_model = validate_output(response.raw_text, schema=schema)
                return validated_model  # type: ignore

            except LLMGuardrailViolation:
                # Security and anti-authority violations fail immediately without retry
                raise

            except (LLMTimeoutError, LLMProviderError, LLMOutputValidationError) as exc:
                last_error = exc
                sanitized_msg = sanitize_log_message(str(exc))
                logger.warning(
                    "LLM generation attempt %d/%d failed: %s",
                    attempt,
                    max_attempts,
                    sanitized_msg,
                )

                if attempt < max_attempts:
                    await asyncio.sleep(self.backoff_factor * (2 ** (attempt - 1)))
                else:
                    raise last_error

            except Exception as exc:
                sanitized_msg = sanitize_log_message(str(exc))
                logger.error("Unexpected LLM invocation failure: %s", sanitized_msg)
                raise LLMProviderError(f"Unexpected LLM error: {sanitized_msg}") from exc

        if last_error:
            raise last_error
        raise LLMError("Exhausted retries without result")

    async def generate_text(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 1024,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Generate raw text output subject to safety guardrails.
        """
        validated_prompt = validate_input(prompt)
        validated_sys_prompt = validate_input(system_prompt) if system_prompt else None

        temp = temperature if temperature is not None else self.temperature
        to = timeout if timeout is not None else self.timeout

        response = await self.provider.generate(
            prompt=validated_prompt,
            system_prompt=validated_sys_prompt,
            temperature=temp,
            max_tokens=max_tokens,
            timeout=to,
        )
        # Validate output length and absence of authority overrides
        validate_output(response.raw_text)
        return response.raw_text
