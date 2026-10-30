import abc
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from .base import BaseAgent
from ..schemas.agent_messages import AgentMessage, AgentStatus
from ..schemas.resource_request import (
    FORBIDDEN_PII_KEYWORDS,
    LocationArea,
    ResourceRequest,
    ResourceType,
    UrgencyLevel,
)

# Textual PII patterns (names, identification numbers, addresses)
PII_REGEX_PATTERNS = [
    r"\bpatient\s*(?:name|id|mrn)?\s*[:=]\s*[A-Za-z0-9_-]+",
    r"\bssn\s*[:=]?\s*\d{3}-\d{2}-\d{4}\b",
    r"\bmrn\s*[:=]?\s*[A-Za-z0-9_-]+\b",
    r"\baadhaar\s*[:=]?\s*\d{4}\s*\d{4}\s*\d{4}\b",
    r"\bdob\s*[:=]?\s*\d{4}-\d{2}-\d{2}\b",
]

WORD_TO_NUMBER = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twenty": 20,
    "fifty": 50,
    "hundred": 100,
}

RESOURCE_TYPE_SYNONYMS: dict[ResourceType, list[str]] = {
    ResourceType.ICU_BED: ["icu bed", "intensive care bed", "critical care bed", "icu"],
    ResourceType.HOSPITAL_BED: ["hospital bed", "general bed", "inpatient bed", "ward bed", "hospital beds", "beds"],
    ResourceType.BLOOD_UNIT: ["blood unit", "blood units", "blood bag", "units of blood", "prbc", "blood"],
    ResourceType.DIAGNOSTIC_SLOT: ["diagnostic slot", "diagnostic test", "mri scan", "ct scan", "mri", "ct", "lab test", "ultrasound scan", "diagnostic slots", "diagnostic"],
    ResourceType.AMBULANCE: ["ambulance", "paramedic transport", "emergency vehicle", "ambulances"],
    ResourceType.MEDICINE: ["medicine", "medication", "pharmaceutical", "vials", "tablets", "doses"],
    ResourceType.OXYGEN_CYLINDER: ["oxygen cylinder", "oxygen tank", "o2 cylinder", "oxygen cylinders"],
    ResourceType.VENTILATOR: ["ventilator", "mechanical ventilator", "bipap", "ventilators"],
}


class BaseRequestParser(abc.ABC):
    """Abstract interface decoupling natural language parsing from agent logic."""

    @abc.abstractmethod
    def parse(
        self,
        text: str,
        requester_institution_id: str,
        default_deadline_hours: int = 24,
    ) -> dict[str, Any]:
        """Parse natural language query into extraction results and missing fields."""
        raise NotImplementedError


class DeterministicRequestParser(BaseRequestParser):
    """
    Rule-based deterministic extractor for healthcare resource requirements.
    
    Guarantees:
    - Never fabricates missing quantities or locations.
    - Strictly rejects patient identifying information (PII).
    - Detects ambiguous multi-resource requests.
    """

    def parse(
        self,
        text: str,
        requester_institution_id: str,
        default_deadline_hours: int = 24,
    ) -> dict[str, Any]:
        raw_text = text.strip() if text else ""
        if not raw_text:
            raise ValueError("Natural-language request text cannot be empty")

        # 1. PII Safety Check
        for pattern in PII_REGEX_PATTERNS:
            if re.search(pattern, raw_text, re.IGNORECASE):
                raise ValueError("Privacy violation: input contains forbidden patient identifying information")

        for pii_keyword in FORBIDDEN_PII_KEYWORDS:
            if re.search(rf"\b{pii_keyword}\b", raw_text, re.IGNORECASE):
                raise ValueError(f"Privacy violation: forbidden key '{pii_keyword}' detected in natural language input")

        lowered = raw_text.lower()

        # 2. Extract Resource Type & Detect Ambiguity
        detected_types: list[ResourceType] = []
        for r_type, synonyms in RESOURCE_TYPE_SYNONYMS.items():
            for synonym in synonyms:
                # Use word-boundary matching
                if re.search(rf"\b{re.escape(synonym)}\b", lowered):
                    if r_type not in detected_types:
                        detected_types.append(r_type)
                    break

        # Disambiguation: if both hospital_bed and icu_bed matched because of "bed", prefer icu_bed
        if ResourceType.ICU_BED in detected_types and ResourceType.HOSPITAL_BED in detected_types:
            detected_types.remove(ResourceType.HOSPITAL_BED)

        resource_type: Optional[ResourceType] = None
        ambiguous = False
        if len(detected_types) == 1:
            resource_type = detected_types[0]
        elif len(detected_types) > 1:
            ambiguous = True

        # 3. Extract Postal Code and Service Area early so they don't pollute quantity or city matching
        postal_match = re.search(r"\b(\d{5,6})\b", raw_text)
        postal_code = postal_match.group(1) if postal_match else None

        area_match = re.search(r"\b(?:service area|district)\s+([A-Za-z0-9\s-]{3,30})\b", raw_text, re.IGNORECASE)
        service_area = area_match.group(1).strip() if area_match else None

        # Mask postal codes and deadline phrases from quantity matching
        qty_text = lowered
        if postal_code:
            qty_text = re.sub(rf"\b{postal_code}\b", "", qty_text)
        qty_text = re.sub(r"\b(?:within|in|by|next)\s+\d+\s*(?:hours?|hrs?|h)\b", "", qty_text)

        # 4. Extract Quantity (written numbers or digits)
        quantity: Optional[int] = None
        for word, val in WORD_TO_NUMBER.items():
            if re.search(rf"\b{word}\b", qty_text):
                quantity = val
                break

        if quantity is None:
            digit_match = re.search(r"\b([1-9]\d{0,3})\s*(?:units?|beds?|slots?|cylinders?|ventilators?|bags?|ambulances?|doses?|vials?)?\b", qty_text)
            if digit_match:
                try:
                    qty_val = int(digit_match.group(1))
                    if 0 < qty_val <= 10000:
                        quantity = qty_val
                except ValueError:
                    pass

        # 5. Extract Urgency
        urgency: UrgencyLevel = UrgencyLevel.ROUTINE
        if re.search(r"\b(critical|stat|emergency|life[-\s]?threatening|code\s*blue)\b", lowered):
            urgency = UrgencyLevel.CRITICAL
        elif re.search(r"\b(urgent(?:ly)?|asap|priority|immediate(?:ly)?|fast|high\s+urgency)\b", lowered):
            urgency = UrgencyLevel.URGENT
        elif re.search(r"\b(routine|standard|elective|normal|scheduled|low\s+urgency)\b", lowered):
            urgency = UrgencyLevel.ROUTINE

        # 6. Extract City Location (excluding boundary words like "within", "hours", etc.)
        city_candidate = None
        # Match capitalized city name after prepositions
        loc_matches = re.finditer(r"\b(?:in|at|near|around)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b", raw_text)
        stop_words = {"postal", "code", "within", "hours", "urgent", "critical", "routine", "emergency", "service"}
        for match in loc_matches:
            cand = match.group(1).strip()
            # Filter out non-location words
            cand_words = set(cand.lower().split())
            if not cand_words.intersection(stop_words):
                city_candidate = cand
                break

        location: Optional[LocationArea] = None
        if city_candidate or postal_code or service_area:
            location = LocationArea(
                city=city_candidate,
                postal_code=postal_code,
                service_area=service_area,
            )

        # 7. Extract Deadline / Delivery window
        time_match = re.search(r"\b(?:within|in|by|next)\s+(\d+)\s*(?:hours?|hrs?|h)\b", lowered)
        required_by_hours = int(time_match.group(1)) if time_match else default_deadline_hours

        # 7. Identify Missing / Ambiguous Information
        missing_fields: list[str] = []
        if resource_type is None:
            missing_fields.append("resource_type")
        if quantity is None:
            missing_fields.append("quantity")
        if location is None:
            missing_fields.append("location")

        return {
            "resource_type": resource_type,
            "quantity": quantity,
            "urgency": urgency,
            "location": location,
            "required_by_hours": required_by_hours,
            "ambiguous": ambiguous,
            "detected_types": detected_types,
            "missing_fields": missing_fields,
            "requester_institution_id": requester_institution_id,
        }


class RequestAgent(BaseAgent):
    """
    Autonomous Request Agent.
    
    Transforms natural-language user requests into validated, privacy-preserving
    ResourceRequest models. Rejects PII and queries users when required data is missing.
    """

    def __init__(
        self,
        name: str = "RequestAgent",
        parser: Optional[BaseRequestParser] = None,
    ) -> None:
        super().__init__(name=name)
        self.parser: BaseRequestParser = parser or DeterministicRequestParser()

    async def process(self, message: AgentMessage) -> dict[str, Any]:
        """
        Process an incoming request message containing natural language text.
        
        Expected payload keys:
            - text (or query / raw_request): Natural language string.
            - requester_institution_id (optional): Requesting institution ID (default: "hospital-a").
        """
        payload = message.payload or {}
        raw_text = payload.get("text") or payload.get("query") or payload.get("raw_request")

        if not raw_text or not str(raw_text).strip():
            raise ValueError("RequestAgent requires 'text', 'query', or 'raw_request' in message payload")

        requester_id = payload.get("requester_institution_id") or "hospital-a"

        # 1. Parse via Decoupled Parser (async or sync)
        if hasattr(self.parser, "parse_async"):
            parsed = await self.parser.parse_async(str(raw_text), requester_institution_id=requester_id)
        else:
            parsed = self.parser.parse(str(raw_text), requester_institution_id=requester_id)

        # 2. Check for Ambiguity
        if parsed.get("ambiguous"):
            detected = [t.value for t in parsed.get("detected_types", [])]
            self.set_status(AgentStatus.WAITING_FOR_APPROVAL)
            return {
                "success": False,
                "clarification_needed": True,
                "reason": "ambiguous_resource_type",
                "missing_fields": ["resource_type"],
                "detected_types": detected,
                "clarification_prompt": (
                    f"Your request mentions multiple resource types ({', '.join(detected)}). "
                    "Please specify one resource type per coordination workflow."
                ),
            }

        # 3. Check for Missing Mandatory Information
        missing_fields = parsed.get("missing_fields", [])
        if missing_fields:
            self.set_status(AgentStatus.WAITING_FOR_APPROVAL)
            prompts = []
            if "resource_type" in missing_fields:
                prompts.append("What specific healthcare resource is required (e.g. ICU bed, blood unit, diagnostic slot)?")
            if "quantity" in missing_fields:
                prompts.append("How many units or slots are required?")
            if "location" in missing_fields:
                prompts.append("What is the service area, city, or postal code for coordination?")

            return {
                "success": False,
                "clarification_needed": True,
                "reason": "missing_information",
                "missing_fields": missing_fields,
                "clarification_prompt": " ".join(prompts),
            }

        # 4. Construct Validated ResourceRequest
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=parsed["required_by_hours"])

        resource_request = ResourceRequest(
            workflow_id=message.workflow_id,
            requester_institution_id=parsed["requester_institution_id"],
            resource_type=parsed["resource_type"],
            quantity=parsed["quantity"],
            urgency=parsed["urgency"],
            location=parsed["location"],
            required_by=deadline,
            required_by_hours=parsed["required_by_hours"],
            created_at=now,
        )

        return {
            "success": True,
            "clarification_needed": False,
            "resource_request": resource_request.model_dump(),
        }
