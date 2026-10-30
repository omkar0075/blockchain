import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Optional
import httpx
from pydantic import BaseModel

from .guardrails import sanitize_log_message
from .schemas import LLMProviderError, LLMResponse, LLMTimeoutError

logger = logging.getLogger("medway.llm.provider")


class LLMProvider(ABC):
    """Abstract interface defining the standardized LLM communication contract."""

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        response_schema: Optional[type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> LLMResponse:
        """Execute an asynchronous model completion call."""
        pass


class MockLLMProvider(LLMProvider):
    """
    Mock LLM Provider for hermetic testing, local evaluation, and offline operation.
    Requires zero external network access and zero API keys.
    """

    def __init__(
        self,
        canned_responses: Optional[list[str]] = None,
        default_json: Optional[dict[str, Any]] = None,
        should_timeout: bool = False,
        should_error: bool = False,
        error_message: str = "Simulated upstream provider outage (HTTP 503)",
        delay_seconds: float = 0.0,
    ) -> None:
        self.canned_responses = list(canned_responses) if canned_responses else []
        self.default_json = default_json
        self.should_timeout = should_timeout
        self.should_error = should_error
        self.error_message = error_message
        self.delay_seconds = delay_seconds
        self.call_count = 0
        self.last_prompt: Optional[str] = None
        self.last_system_prompt: Optional[str] = None

    def add_response(self, text_or_dict: Any) -> None:
        """Queue a canned response for upcoming calls."""
        if isinstance(text_or_dict, dict):
            self.canned_responses.append(json.dumps(text_or_dict))
        else:
            self.canned_responses.append(str(text_or_dict))

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        response_schema: Optional[type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt

        start = time.perf_counter()

        if self.delay_seconds > 0:
            if self.delay_seconds > timeout or self.should_timeout:
                await asyncio.sleep(min(timeout + 0.05, 0.5))
                raise LLMTimeoutError(f"Mock request timed out after {timeout}s limit")
            await asyncio.sleep(self.delay_seconds)

        if self.should_timeout:
            raise LLMTimeoutError(f"Mock request timed out after {timeout}s limit")

        if self.should_error:
            raise LLMProviderError(self.error_message)

        duration_ms = (time.perf_counter() - start) * 1000.0

        # 1. Pop from canned responses if available
        if self.canned_responses:
            raw_text = self.canned_responses.pop(0)
            return LLMResponse(
                raw_text=raw_text,
                model="mock-deterministic",
                prompt_tokens=len(prompt.split()),
                completion_tokens=len(raw_text.split()),
                duration_ms=round(duration_ms, 2),
            )

        # 2. Return configured default JSON
        if self.default_json is not None:
            raw_text = json.dumps(self.default_json)
            return LLMResponse(
                raw_text=raw_text,
                model="mock-deterministic",
                prompt_tokens=len(prompt.split()),
                completion_tokens=len(raw_text.split()),
                duration_ms=round(duration_ms, 2),
            )

        # 3. Dynamic synthesis based on schema
        raw_text = "{}"
        if response_schema is not None:
            # Generate a valid minimal fixture matching target fields
            fields = response_schema.model_fields
            synthetic: dict[str, Any] = {}
            for fname, fval in fields.items():
                annotation = fval.annotation
                if annotation in (str, Optional[str]):
                    synthetic[fname] = "mock_value"
                elif annotation in (int, Optional[int]):
                    synthetic[fname] = 1
                elif annotation in (float, Optional[float]):
                    synthetic[fname] = 1.0
                elif annotation in (bool, Optional[bool]):
                    synthetic[fname] = True
                elif annotation in (list, list[str]):
                    synthetic[fname] = ["mock_item"]
                else:
                    synthetic[fname] = None
            raw_text = json.dumps(synthetic)
        else:
            raw_text = json.dumps({"response": "Mock evaluation completed successfully."})

        return LLMResponse(
            raw_text=raw_text,
            model="mock-deterministic",
            prompt_tokens=len(prompt.split()),
            completion_tokens=len(raw_text.split()),
            duration_ms=round(duration_ms, 2),
        )


class GeminiLLMProvider(LLMProvider):
    """Google Gemini model provider using REST API with strict credential masking."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_name: str = "gemini-2.5-flash",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model_name = model_name
        self.base_url = base_url.rstrip("/")

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        response_schema: Optional[type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMProviderError("Gemini API key is not configured")

        url = f"{self.base_url}/models/{self.model_name}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}

        contents = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": f"SYSTEM INSTRUCTION: {system_prompt}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood. I will strictly follow these instructions."}]})

        contents.append({"role": "user", "parts": [{"text": prompt}]})

        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if response_schema is not None:
            generation_config["responseMimeType"] = "application/json"

        payload = {
            "contents": contents,
            "generationConfig": generation_config,
        }

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                duration_ms = (time.perf_counter() - start) * 1000.0

                if resp.status_code != 200:
                    err_msg = sanitize_log_message(
                        f"Gemini API error {resp.status_code}: {resp.text}",
                        api_keys=[self.api_key],
                    )
                    raise LLMProviderError(err_msg)

                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    raise LLMProviderError("Gemini API returned zero candidates")

                parts = candidates[0].get("content", {}).get("parts", [])
                raw_text = parts[0].get("text", "") if parts else ""

                usage = data.get("usageMetadata", {})
                return LLMResponse(
                    raw_text=raw_text,
                    model=self.model_name,
                    prompt_tokens=usage.get("promptTokenCount"),
                    completion_tokens=usage.get("candidatesTokenCount"),
                    duration_ms=round(duration_ms, 2),
                )
        except httpx.TimeoutException as te:
            raise LLMTimeoutError(f"Gemini API request timed out after {timeout}s") from te
        except Exception as e:
            if isinstance(e, (LLMTimeoutError, LLMProviderError)):
                raise
            clean_err = sanitize_log_message(str(e), api_keys=[self.api_key])
            raise LLMProviderError(f"Gemini API request failed: {clean_err}") from e


class OpenAILikeLLMProvider(LLMProvider):
    """Generic OpenAI-compatible provider."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        api_key: Optional[str] = None,
        model_name: str = "llama3",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model_name = model_name

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        response_schema: Optional[type[BaseModel]] = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_schema is not None:
            payload["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                duration_ms = (time.perf_counter() - start) * 1000.0

                if resp.status_code != 200:
                    err_msg = sanitize_log_message(
                        f"OpenAI-like API error {resp.status_code}: {resp.text}",
                        api_keys=[self.api_key],
                    )
                    raise LLMProviderError(err_msg)

                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    raise LLMProviderError("Provider returned zero choices")

                raw_text = choices[0].get("message", {}).get("content", "")
                usage = data.get("usage", {})
                return LLMResponse(
                    raw_text=raw_text,
                    model=self.model_name,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    duration_ms=round(duration_ms, 2),
                )
        except httpx.TimeoutException as te:
            raise LLMTimeoutError(f"Request timed out after {timeout}s") from te
        except Exception as e:
            if isinstance(e, (LLMTimeoutError, LLMProviderError)):
                raise
            clean_err = sanitize_log_message(str(e), api_keys=[self.api_key])
            raise LLMProviderError(f"Provider request failed: {clean_err}") from e


def get_llm_provider(
    provider_type: Optional[str] = None,
    api_key: Optional[str] = None,
    **kwargs: Any,
) -> LLMProvider:
    """
    Factory function returning the configured LLMProvider.
    Defaults to MockLLMProvider when no API key is set.
    """
    p_type = (provider_type or os.getenv("LLM_PROVIDER", "mock")).lower()

    if p_type == "gemini":
        key = api_key or os.getenv("GEMINI_API_KEY")
        if not key:
            logger.warning("No GEMINI_API_KEY found; falling back to MockLLMProvider")
            return MockLLMProvider(**kwargs)
        return GeminiLLMProvider(api_key=key, **kwargs)

    elif p_type in ("openai", "openai-like", "local"):
        return OpenAILikeLLMProvider(api_key=api_key, **kwargs)

    return MockLLMProvider(**kwargs)
