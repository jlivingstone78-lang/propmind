import os
import base64 as b64_stdlib
import logging
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")
REDIRECT_URI = os.environ.get(
    "OAUTH_REDIRECT_URI",
    "https://propmind-production.up.railway.app/api/oauth/callback",
)

# Holds the in-progress flow between /api/oauth/url and /api/oauth/callback
_pending_flow: Flow | None = None


def _client_config() -> dict:
    return {
        "web": {
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _load_token_from_env() -> bool:
    """Decode GMAIL_TOKEN_JSON env var (base64) into TOKEN_FILE. Returns True if written."""
    raw = os.environ.get("GMAIL_TOKEN_JSON", "")
    if not raw:
        return False
    try:
        token_json = b64_stdlib.b64decode(raw).decode("utf-8")
        with open(TOKEN_FILE, "w") as f:
            f.write(token_json)
        logger.info("Loaded OAuth token from GMAIL_TOKEN_JSON env var → %s", TOKEN_FILE)
        return True
    except Exception as exc:
        logger.warning("Failed to decode GMAIL_TOKEN_JSON: %s", exc)
        return False


def get_credentials() -> Credentials:
    creds = None

    if not os.path.exists(TOKEN_FILE):
        _load_token_from_env()

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    raise RuntimeError(
        "Gmail not authorised. Call GET /api/oauth/url then visit the returned URL."
    )


def get_auth_url() -> str:
    """Return a Google OAuth consent URL and stash the flow for the callback."""
    global _pending_flow
    _pending_flow = Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = _pending_flow.authorization_url(
        prompt="consent",
        access_type="offline",
    )
    logger.info("OAuth URL generated — redirect_uri=%s", REDIRECT_URI)
    return auth_url


def complete_auth_from_callback(code: str) -> str:
    """Exchange the code delivered by Google's redirect. Returns base64 token for Railway env var."""
    global _pending_flow
    if _pending_flow is None:
        raise RuntimeError("No pending OAuth flow — call GET /api/oauth/url first.")

    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # redirect_uri may be http in dev
    _pending_flow.fetch_token(code=code)
    creds = _pending_flow.credentials
    _save_token(creds)
    _pending_flow = None

    encoded = b64_stdlib.b64encode(creds.to_json().encode()).decode()
    logger.info("OAuth complete. Copy gmail_token_b64 into Railway GMAIL_TOKEN_JSON env var.")
    return encoded


def _save_token(creds: Credentials):
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    logger.info("OAuth token saved to %s", TOKEN_FILE)


def build_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds)


def _decode_body(payload: dict) -> str:
    body = ""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body = b64_stdlib.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body = b64_stdlib.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    break
        if not body:
            for part in payload.get("parts", []):
                if part.get("mimeType") == "text/html":
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body = b64_stdlib.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                        break

    return body.strip()


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_unread_emails(service) -> list[dict]:
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    query = "is:unread"
    if gmail_address:
        query += f" to:{gmail_address}"

    result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = result.get("messages", [])

    emails = []
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()

        headers = msg.get("payload", {}).get("headers", [])
        subject = _header(headers, "Subject")
        from_raw = _header(headers, "From")
        date_raw = _header(headers, "Date")

        sender_name, sender_email = parseaddr(from_raw)
        if not sender_name:
            sender_name = sender_email

        try:
            received_at = parsedate_to_datetime(date_raw).astimezone(timezone.utc)
        except Exception:
            received_at = datetime.now(timezone.utc)

        body = _decode_body(msg.get("payload", {}))

        emails.append(
            {
                "gmail_message_id": msg_ref["id"],
                "sender_name": sender_name,
                "sender_email": sender_email,
                "subject": subject,
                "body": body,
                "received_at": received_at.isoformat(),
            }
        )

    return emails


def mark_as_read(service, message_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()
