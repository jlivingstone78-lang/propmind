import base64
import json
import os
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import format_datetime, parsedate_to_datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

import database
from database import get_all_gmail_ids, clear_all_emails, match_property
import ai_service
import scheduler
from gmail_service import (
    build_service,
    fetch_unread_emails,
    mark_as_read,
    get_auth_url,
    complete_auth_from_callback,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

TENANT_ID = os.environ.get("TENANT_ID", "dev")

_gmail_service = None


def poll_inbox():
    global _gmail_service
    logger.info("Polling Gmail inbox for tenant=%s", TENANT_ID)
    try:
        if _gmail_service is None:
            _gmail_service = build_service()

        emails = fetch_unread_emails(_gmail_service)
        logger.info("Found %d unread email(s)", len(emails))

        for email in emails:
            if database.email_exists(email["gmail_message_id"]):
                logger.debug("Skipping already-processed message %s", email["gmail_message_id"])
                continue

            try:
                ai_result = ai_service.categorise_and_draft(
                    subject=email["subject"],
                    body=email["body"],
                    sender_name=email["sender_name"],
                )
            except Exception as exc:
                logger.error("AI processing failed for %s: %s", email["gmail_message_id"], exc)
                ai_result = {
                    "category": "GENERAL",
                    "urgency": "LOW",
                    "draft_response": "",
                    "confidence_score": 0,
                }

            record = {
                **email,
                "tenant_id": TENANT_ID,
                "category": ai_result["category"],
                "urgency": ai_result["urgency"],
                "draft_response": ai_result["draft_response"],
                "confidence_score": ai_result["confidence_score"],
                "property_hints": ai_result.get("property_hints", ""),
                "property_match_status": ai_result.get("property_match_status", "UNMATCHED"),
                "matched_property": "",
                "status": "PENDING",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }

            database.insert_email(record)
            mark_as_read(_gmail_service, email["gmail_message_id"])
            logger.info(
                "Processed email %s — %s / %s",
                email["gmail_message_id"],
                ai_result["category"],
                ai_result["urgency"],
            )

    except Exception as exc:
        logger.error("poll_inbox error: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    logger.info("Database initialised")

    poll_interval = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
    scheduler.start(poll_inbox, interval_minutes=poll_interval)

    yield

    scheduler.stop()


app = FastAPI(title="PropMind API", lifespan=lifespan)

_base_dir = os.path.dirname(__file__)
app.mount("/data", StaticFiles(directory=os.path.join(_base_dir, "data")), name="data")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Root + Health ─────────────────────────────────────────────────────────────

@app.get("/", response_class=FileResponse)
def root():
    return os.path.join(_base_dir, "index.html")


@app.get("/health")
def health():
    count = database.count_emails(TENANT_ID)
    return {"status": "ok", "tenant": TENANT_ID, "email_count": count}


# ── Email CRUD ────────────────────────────────────────────────────────────────

@app.get("/api/emails")
def list_emails(
    tenant_id: str = Query(default=None),
    status: str = Query(default=None),
    property_match_status: str = Query(default=None),
):
    tid = tenant_id or TENANT_ID
    emails = database.get_emails(tid, status)
    if property_match_status:
        emails = [e for e in emails if e.get("property_match_status") == property_match_status]
    return emails


@app.get("/api/emails/{email_id}")
def get_email(email_id: int):
    email = database.get_email_by_id(email_id)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@app.post("/api/emails/{email_id}/send")
def send_email(email_id: int):
    email = database.update_status(email_id, "SENT")
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@app.post("/api/emails/{email_id}/assign")
def assign_email(email_id: int):
    email = database.update_status(email_id, "ASSIGNED")
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@app.post("/api/emails/{email_id}/ignore")
def ignore_email(email_id: int):
    email = database.update_status(email_id, "IGNORED")
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


class MatchPropertyRequest(BaseModel):
    matched_property: str


@app.post("/api/emails/{email_id}/match-property")
def match_property_endpoint(email_id: int, body: MatchPropertyRequest):
    """Manually link an email to a known property address."""
    email = match_property(email_id, body.matched_property)
    if not email:
        raise HTTPException(status_code=404, detail="Email not found")
    return email


@app.post("/api/trigger-poll")
def trigger_poll():
    logger.info("Manual poll triggered via API")
    poll_inbox()
    count = database.count_emails(TENANT_ID)
    return {"status": "ok", "message": "Poll complete", "email_count": count}


# ── Gmail OAuth ───────────────────────────────────────────────────────────────

@app.get("/api/oauth/url")
def oauth_url():
    """Step 1: returns the Google consent URL. Visit it in a browser."""
    url = get_auth_url()
    return {
        "auth_url": url,
        "instructions": "Visit auth_url in your browser and approve access. Google will redirect back automatically.",
    }


@app.get("/api/oauth/callback")
def oauth_callback(request: Request, code: str = Query(default=None), error: str = Query(default=None)):
    """Step 2 (automatic): Google redirects here after the user approves access."""
    global _gmail_service

    if error:
        return HTMLResponse(
            f"<h2>OAuth error: {error}</h2><p>Close this tab and try again.</p>",
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            "<h2>Missing code parameter.</h2><p>Close this tab and try again.</p>",
            status_code=400,
        )

    try:
        token_b64 = complete_auth_from_callback(code)
        _gmail_service = None  # force rebuild on next poll
        return HTMLResponse(f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:40px auto;padding:0 20px">
<h2>✅ Gmail authorised!</h2>
<p>PropMind can now read the <strong>propmind.test@gmail.com</strong> inbox.</p>
<h3>One more step — make this permanent on Railway:</h3>
<ol>
<li>Go to your Railway project → <strong>Variables</strong></li>
<li>Add a new variable: <code>GMAIL_TOKEN_JSON</code></li>
<li>Paste the value below as the variable value:</li>
</ol>
<textarea rows="4" style="width:100%;font-size:11px;word-break:break-all">{token_b64}</textarea>
<p>This prevents you needing to re-authorise after every Railway redeploy.</p>
<p><strong>You can close this tab.</strong></p>
</body></html>
""")
    except RuntimeError as exc:
        return HTMLResponse(
            f"<h2>Error: {exc}</h2><p>Go back and call /api/oauth/url again.</p>",
            status_code=400,
        )


# ── Demo seeding ──────────────────────────────────────────────────────────────

DUMMY_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "dummy_emails.json")
DUMMY_AMBIGUOUS_PATH = os.path.join(os.path.dirname(__file__), "data", "dummy_emails_ambiguous.json")
_SEED_HEADER = "X-PropMind-Seed"


def _insert_raw(service, raw: str) -> str:
    result = service.users().messages().insert(
        userId="me",
        internalDateSource="dateHeader",
        body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]},
    ).execute()
    return result["id"]


def _build_raw(email: dict, to_address: str) -> str:
    msg = MIMEText(email["body"], "plain", "utf-8")
    msg["To"] = to_address
    msg["From"] = f"{email['from_name']} <{email['from_email']}>"
    msg["Subject"] = email["subject"]
    msg[_SEED_HEADER] = "true"
    try:
        ts = email.get("timestamp", "")
        dt = datetime.fromisoformat(ts) if "T" in ts else parsedate_to_datetime(ts)
        msg["Date"] = format_datetime(dt)
    except Exception:
        msg["Date"] = format_datetime(datetime.now(timezone.utc))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _find_gmail_ids_to_trash(service) -> list[str]:
    """Return IDs from the DB plus any in Gmail whose sender matches a known dummy address."""
    ids = set(get_all_gmail_ids())

    with open(DUMMY_DATA_PATH) as f:
        dummy = json.load(f)
    known_senders = {e["from_email"].lower() for e in dummy}

    # List all inbox + sent messages, filter by known sender in Python
    for label in ("INBOX", "SENT"):
        page_token = None
        while True:
            kwargs = {"userId": "me", "labelIds": [label], "maxResults": 200}
            if page_token:
                kwargs["pageToken"] = page_token
            result = service.users().messages().list(**kwargs).execute()
            for m in result.get("messages", []):
                detail = service.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From"]
                ).execute()
                from_header = next(
                    (h["value"] for h in detail.get("payload", {}).get("headers", [])
                     if h["name"].lower() == "from"),
                    ""
                ).lower()
                if any(s in from_header for s in known_senders):
                    ids.add(m["id"])
            page_token = result.get("nextPageToken")
            if not page_token:
                break

    return list(ids)


@app.post("/api/reset-demo")
def reset_demo():
    """Trash all seeded emails from Gmail and wipe the database. Clean slate for demos."""
    global _gmail_service
    if _gmail_service is None:
        _gmail_service = build_service()

    to_trash = _find_gmail_ids_to_trash(_gmail_service)
    trashed, skipped = 0, 0
    for mid in to_trash:
        try:
            _gmail_service.users().messages().trash(userId="me", id=mid).execute()
            trashed += 1
            time.sleep(0.1)
        except Exception:
            skipped += 1

    clear_all_emails()
    logger.info("Demo reset: trashed %d Gmail messages, wiped DB", trashed)
    return {
        "status": "ok",
        "gmail_messages_trashed": trashed,
        "gmail_messages_skipped": skipped,
        "database_cleared": True,
        "next_step": "Call POST /api/seed-inbox then POST /api/trigger-poll to reload.",
    }


@app.get("/api/debug/inbox")
def debug_inbox():
    """Temporary: list what Railway sees in the Gmail inbox (first 50 messages)."""
    global _gmail_service
    if _gmail_service is None:
        _gmail_service = build_service()
    result = _gmail_service.users().messages().list(
        userId="me", maxResults=50
    ).execute()
    messages = result.get("messages", [])
    rows = []
    for m in messages:
        detail = _gmail_service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject"]
        ).execute()
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        rows.append({
            "id": m["id"],
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "labels": detail.get("labelIds", []),
        })
    return {"total": len(rows), "messages": rows}


@app.post("/api/seed-inbox")
def seed_inbox(ambiguous: bool = Query(default=False)):
    """Insert dummy tenant emails into the Gmail inbox as UNREAD. Pass ?ambiguous=true to seed the property-ambiguous set."""
    global _gmail_service
    if _gmail_service is None:
        _gmail_service = build_service()

    to_address = os.environ.get("GMAIL_ADDRESS", "propmind.test@gmail.com")
    data_path = DUMMY_AMBIGUOUS_PATH if ambiguous else DUMMY_DATA_PATH

    with open(data_path) as f:
        emails = json.load(f)

    inserted, failed = [], []
    for email in emails:
        try:
            raw = _build_raw(email, to_address)
            msg_id = _insert_raw(_gmail_service, raw)
            inserted.append({"subject": email["subject"], "from": email["from_name"], "gmail_id": msg_id})
            time.sleep(0.25)
        except Exception as exc:
            failed.append({"subject": email["subject"], "error": str(exc)})

    return {
        "status": "ok",
        "inserted": len(inserted),
        "failed": len(failed),
        "messages": inserted,
        "errors": failed,
        "next_step": "Call POST /api/trigger-poll to process them immediately, or wait up to 5 minutes.",
    }
