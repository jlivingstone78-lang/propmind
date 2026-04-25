import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

import database
import ai_service
import scheduler
from gmail_service import build_service, fetch_unread_emails, mark_as_read

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


@app.get("/health")
def health():
    count = database.count_emails(TENANT_ID)
    return {"status": "ok", "tenant": TENANT_ID, "email_count": count}


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
