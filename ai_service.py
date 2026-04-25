import os
import json
import logging
from anthropic import Anthropic

logger = logging.getLogger(__name__)

_client: Anthropic | None = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


SYSTEM_PROMPT = """You are an AI assistant for a property management company.

For each email you receive, return a JSON object with exactly these fields:

1. category — exactly one of: MAINTENANCE, LEASE, PAYMENT, COMPLAINT, INSPECTION, GENERAL
2. urgency — exactly one of: LOW, MEDIUM, HIGH, URGENT
3. draft_response — a professional reply the property manager can review and send. If the property cannot be identified, the draft should politely ask the tenant to confirm their property address before actioning the request.
4. confidence_score — integer 0-100 reflecting how confident you are in the categorisation
5. property_hints — any address clues found in the email: full address, street name, unit/apartment number, suburb, floor, or descriptive references like "the corner unit" or "the house in Chermside". Return an empty string if none exist.
6. property_match_status — exactly one of:
   - "MATCHED" if a full street address (number + street + suburb) is clearly provided
   - "AMBIGUOUS" if partial clues exist (e.g. unit number without street, suburb only, "the complex", "my two-bedroom")
   - "UNMATCHED" if the email contains no property information whatsoever

Respond with valid JSON only. No markdown, no explanation outside the JSON object.

Example response shape:
{
  "category": "MAINTENANCE",
  "urgency": "HIGH",
  "draft_response": "Dear...",
  "confidence_score": 95,
  "property_hints": "Unit 9",
  "property_match_status": "AMBIGUOUS"
}"""


def categorise_and_draft(subject: str, body: str, sender_name: str) -> dict:
    user_message = (
        f"From: {sender_name}\n"
        f"Subject: {subject}\n\n"
        f"{body}"
    )

    message = get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    raw = message.content[0].text.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Claude returned non-JSON, using fallback: %s", raw[:200])
        result = {
            "category": "GENERAL",
            "urgency": "LOW",
            "draft_response": raw,
            "confidence_score": 0,
            "property_hints": "",
            "property_match_status": "UNMATCHED",
        }

    return {
        "category": result.get("category", "GENERAL"),
        "urgency": result.get("urgency", "LOW"),
        "draft_response": result.get("draft_response", ""),
        "confidence_score": int(result.get("confidence_score", 0)),
        "property_hints": result.get("property_hints", ""),
        "property_match_status": result.get("property_match_status", "UNMATCHED"),
    }
