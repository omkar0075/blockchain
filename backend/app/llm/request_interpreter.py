import asyncio
import concurrent.futures
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..agents.request_agent import (
    BaseRequestParser,
    DeterministicRequestParser,
    FORBIDDEN_PII_KEYWORDS,
    PII_REGEX_PATTERNS,
    WORD_TO_NUMBER,
)
from ..schemas.resource_request import LocationArea, ResourceRequest, ResourceType, UrgencyLevel
from .client import LLMClient
from .guardrails import LLMGuardrailViolation, LLMOutputValidationError
from .schemas import LLMError, RequestInterpretationOutput

logger = logging.getLogger("medway.llm.interpreter")

INTERPRETER_SYSTEM_PROMPT = """You are MEDway's Healthcare Logistics Request Interpreter.
Your mission is to accurately interpret natural-language healthcare resource requests into structured parameters.

Supported Resource Types:
- diagnostic_slot (MRI, CT, lab tests, ultrasound)
- icu_bed (ICU, intensive care, critical care)
- hospital_bed (general inpatient, ward bed)
- blood_unit (blood bags, PRBC, units of blood)
- ambulance (paramedic transport, emergency vehicle)
- medicine (pharmaceuticals, vials, doses)
- oxygen_cylinder (oxygen tanks, O2 cylinders)
- ventilator (mechanical ventilators, BiPAP)

Supported Urgency Levels:
- routine (standard, non-emergency)
- urgent (high priority, prompt delivery needed)
- critical (immediate life-threatening emergency)

Extraction & Safety Rules:
1. Quantity: Extract integer quantity. Convert number words ("three", "ten") to digits. If quantity is missing or unspecified, leave null and list in missing_fields.
2. Location: Extract facility city, area, or postal code. If missing, leave null and list in missing_fields.
3. Resource Type: Map to one of the exact supported types. If multiple competing resources are mentioned, set is_ambiguous=true. If an unsupported resource is requested (e.g. pizza, helicopter, organs), set resource_type to null and list in missing_fields.
4. Deadline: Infer required_by_hours based on temporal cues ("tomorrow morning" -> 18, "within 6 hours" -> 6, "today" -> 8).
5. Zero Hallucination: NEVER guess missing quantities, locations, or resource types. If missing, set is_ambiguous=true and formulate a clarification_prompt.
6. Zero PII: Strictly reject and ignore any patient names, IDs, SSNs, or MRNs.

Output must strictly conform to RequestInterpretationOutput."""


class LLMRequestParser(BaseRequestParser):
    """
    LLM-Powered Healthcare Resource Request Parser.

    Combines flexible natural language interpretation with deterministic validation:
    1. Interprets natural language requests using controlled LLM reasoning.
    2. Enforces Pydantic schema validation on extracted outputs.
    3. Strictly blocks patient identifying information (PII).
    4. Rejects unsupported resource categories.
    5. Falls back seamlessly to DeterministicRequestParser when the LLM is unavailable.
    6. Advisory Only: Never reserves capacity, contacts providers, or triggers blockchain operations.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        fallback_parser: Optional[BaseRequestParser] = None,
        enable_fallback: bool = True,
    ) -> None:
        self.client = llm_client or LLMClient()
        self.fallback_parser = fallback_parser or DeterministicRequestParser()
        self.enable_fallback = enable_fallback

    async def parse_async(
        self,
        text: str,
        requester_institution_id: str,
        default_deadline_hours: int = 24,
    ) -> dict[str, Any]:
        """
        Asynchronously interpret a natural language request using the LLM with fallback.
        """
        raw_text = text.strip() if text else ""
        if not raw_text:
            raise ValueError("Natural-language request text cannot be empty")

        # 1. Zero-PII Boundary Check
        self._enforce_pii_check(raw_text)

        # 2. Check for unsupported resource types upfront
        self._validate_supported_resources(raw_text)

        # 3. Controlled LLM Interpretation
        try:
            interpretation: RequestInterpretationOutput = await self.client.generate_structured(
                prompt=f"Healthcare Request from institution '{requester_institution_id}':\n\"{raw_text}\"",
                schema=RequestInterpretationOutput,
                system_prompt=INTERPRETER_SYSTEM_PROMPT,
            )
            return self._build_result_from_llm(
                interpretation=interpretation,
                raw_text=raw_text,
                requester_institution_id=requester_institution_id,
                default_deadline_hours=default_deadline_hours,
            )

        except (LLMError, LLMOutputValidationError, LLMGuardrailViolation) as exc:
            if not self.enable_fallback:
                raise
            logger.warning("LLM request interpretation unavailable (%s); using deterministic fallback", exc)
            return self.fallback_parser.parse(
                text=raw_text,
                requester_institution_id=requester_institution_id,
                default_deadline_hours=default_deadline_hours,
            )
        except Exception as exc:
            if not self.enable_fallback:
                raise
            logger.error("Unexpected error in LLM interpreter (%s); using deterministic fallback", exc)
            return self.fallback_parser.parse(
                text=raw_text,
                requester_institution_id=requester_institution_id,
                default_deadline_hours=default_deadline_hours,
            )

    def parse(
        self,
        text: str,
        requester_institution_id: str,
        default_deadline_hours: int = 24,
    ) -> dict[str, Any]:
        """
        Synchronous wrapper for BaseRequestParser interface compatibility.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Run in background thread pool to avoid blocking the event loop
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run,
                    self.parse_async(text, requester_institution_id, default_deadline_hours),
                ).result(timeout=15.0)
        else:
            return asyncio.run(
                self.parse_async(text, requester_institution_id, default_deadline_hours)
            )

    def _enforce_pii_check(self, text: str) -> None:
        """Reject any patient identifying information before processing."""
        for pattern in PII_REGEX_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                raise ValueError("Privacy violation: input contains forbidden patient identifying information")

        for pii_keyword in FORBIDDEN_PII_KEYWORDS:
            if re.search(rf"\b{pii_keyword}\b", text, re.IGNORECASE):
                raise ValueError(f"Privacy violation: forbidden key '{pii_keyword}' detected in natural language input")

    def _validate_supported_resources(self, text: str) -> None:
        """Reject unsupported resource categories like surgical robots, helicopters, organs."""
        unsupported_patterns = [
            (r"\b(?:surgical\s+robot|da\s+vinci)\b", "surgical_robot"),
            (r"\b(?:helicopter|air\s+ambulance\s+chopper)\b", "helicopter"),
            (r"\b(?:organ|kidney|heart|liver)\s+transplant\b", "organ_transplant"),
            (r"\b(?:pizza|food|catering)\b", "food_delivery"),
        ]
        for pat, name in unsupported_patterns:
            if re.search(pat, text, re.IGNORECASE):
                raise ValueError(
                    f"Unsupported resource type '{name}'. MEDway supports: diagnostic slots, beds, blood units, ambulances, oxygen, and ventilators."
                )

    def _build_result_from_llm(
        self,
        interpretation: RequestInterpretationOutput,
        raw_text: str,
        requester_institution_id: str,
        default_deadline_hours: int,
    ) -> dict[str, Any]:
        """
        Translate structured LLM output into validated RequestAgent dictionary format.
        """
        missing_fields: list[str] = list(interpretation.missing_fields)
        ambiguous = interpretation.is_ambiguous

        # 1. Parse Resource Type
        resource_type: Optional[ResourceType] = None
        if interpretation.resource_type:
            raw_rtype = interpretation.resource_type.lower().replace(" ", "_")
            try:
                resource_type = ResourceType(raw_rtype)
            except ValueError:
                # Check known aliases
                if "diagnostic" in raw_rtype or "slot" in raw_rtype:
                    resource_type = ResourceType.DIAGNOSTIC_SLOT
                elif "icu" in raw_rtype:
                    resource_type = ResourceType.ICU_BED
                elif "bed" in raw_rtype:
                    resource_type = ResourceType.HOSPITAL_BED
                elif "blood" in raw_rtype:
                    resource_type = ResourceType.BLOOD_UNIT
                elif "ambulance" in raw_rtype:
                    resource_type = ResourceType.AMBULANCE
                elif "ventilator" in raw_rtype:
                    resource_type = ResourceType.VENTILATOR
                elif "oxygen" in raw_rtype:
                    resource_type = ResourceType.OXYGEN_CYLINDER
                else:
                    raise ValueError(f"Unsupported resource type '{interpretation.resource_type}'")

        if resource_type is None and not ambiguous:
            missing_fields.append("resource_type")

        # 2. Parse Quantity (supporting written words as fallback)
        quantity: Optional[int] = interpretation.quantity
        if quantity is None:
            # Fallback scan for written numbers in text
            for word, num in WORD_TO_NUMBER.items():
                if re.search(rf"\b{word}\b", raw_text, re.IGNORECASE):
                    quantity = num
                    break

        if quantity is None and "quantity" not in missing_fields:
            missing_fields.append("quantity")

        # 3. Parse Location
        location: Optional[LocationArea] = None
        if interpretation.location:
            loc_str = interpretation.location.strip()
            location = LocationArea(city=loc_str, service_area=loc_str)
        elif "location" not in missing_fields:
            missing_fields.append("location")

        # 4. Parse Urgency
        urgency: UrgencyLevel = UrgencyLevel.URGENT
        if interpretation.urgency:
            u_str = interpretation.urgency.lower()
            if "routine" in u_str or "low" in u_str:
                urgency = UrgencyLevel.ROUTINE
            elif "critical" in u_str or "emergency" in u_str:
                urgency = UrgencyLevel.CRITICAL
            else:
                urgency = UrgencyLevel.URGENT

        # 5. Parse Required Hours
        req_hours = interpretation.required_by_hours or default_deadline_hours

        # Deduplicate missing fields
        unique_missing = sorted(list(set(missing_fields)))

        # Ambiguous is strictly when multiple competing resource types are detected or resource type itself is ambiguous
        ambiguous = False
        if interpretation.is_ambiguous and (resource_type is None or "resource_type" in unique_missing):
            ambiguous = True

        return {
            "resource_type": resource_type,
            "quantity": quantity,
            "urgency": urgency,
            "location": location,
            "required_by_hours": req_hours,
            "ambiguous": ambiguous,
            "detected_types": [resource_type] if resource_type else [],
            "missing_fields": unique_missing,
            "requester_institution_id": requester_institution_id,
            "clarification_prompt": interpretation.clarification_prompt,
            "interpretation": interpretation.model_dump(),
            "is_llm_interpreted": True,
        }
