import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

import database
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    count = database.count_emails(TENANT_ID)
    return {"status": "ok", "tenant": TENANT_ID, "email_count": count}


# ── Email CRUD ────────────────────────────────────────────────────────────────

@app.get("/api/emails")
def list_emails(
    tenant_id: str = Query(default=None),
    status: str = Query(default=None),
):
    tid = tenant_id or TENANT_ID
    return database.get_emails(tid, status)


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
