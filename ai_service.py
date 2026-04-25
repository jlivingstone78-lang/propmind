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


SYSTEM_PROMPT = (
    "You are an AI assistant for a property management company. "
    "Categorise this email as exactly one of: MAINTENANCE, LEASE, "
    "PAYMENT, COMPLAINT, INSPECTION, GENERAL. Then write a "
    "professional draft reply the property manager can review and send. "
    "Respond in JSON: {\"category\": \"...\", \"urgency\": \"LOW|MEDIUM|HIGH|URGENT\", "
    "\"draft_response\": \"...\", \"confidence_score\": 0-100}"
)


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

    # Strip markdown code fences if present
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
        }

    return {
        "category": result.get("category", "GENERAL"),
        "urgency": result.get("urgency", "LOW"),
        "draft_response": result.get("draft_response", ""),
        "confidence_score": int(result.get("confidence_score", 0)),
    }
