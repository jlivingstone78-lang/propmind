import os
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")


def _client_config() -> dict:
    return {
        "installed": {
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def get_credentials() -> Credentials:
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds

    # First-run interactive OAuth
    flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    logger.info("=" * 60)
    logger.info("GMAIL OAUTH REQUIRED")
    logger.info("Visit this URL to authorise PropMind:")
    logger.info(auth_url)
    logger.info("=" * 60)
    print("\n" + "=" * 60)
    print("GMAIL OAUTH REQUIRED")
    print("Visit this URL to authorise PropMind:")
    print(auth_url)
    print("=" * 60)

    code = input("Paste the authorisation code here: ").strip()
    flow.fetch_token(code=code)
    creds = flow.credentials
    _save_token(creds)
    return creds


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
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    break
        # fallback to HTML part if no plain text
        if not body:
            for part in payload.get("parts", []):
                if part.get("mimeType") == "text/html":
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
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
