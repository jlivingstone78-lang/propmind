"""
seed_inbox.py — inserts dummy tenant emails directly into the Gmail inbox.

Uses the Gmail API insert method so messages appear with the correct From
headers and timestamps without needing an SMTP relay. Each message is
inserted as UNREAD so the polling pipeline picks it up automatically.

Usage:
    python3 seed_inbox.py                  # insert all 20 emails
    python3 seed_inbox.py --dry-run        # preview without inserting
    python3 seed_inbox.py --clear          # delete all seeded messages first
"""

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import format_datetime, parsedate_to_datetime

from dotenv import load_dotenv

load_dotenv()

# Resolve path relative to this file so the script works from any cwd
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DUMMY_DATA = os.path.join(SCRIPT_DIR, "data", "dummy_emails.json")
SEED_LABEL = "X-PropMind-Seed"  # marker stored in message headers


def get_service():
    sys.path.insert(0, SCRIPT_DIR)
    from gmail_service import build_service
    return build_service()


def build_raw_message(email: dict, to_address: str) -> str:
    msg = MIMEText(email["body"], "plain", "utf-8")
    msg["To"] = to_address
    msg["From"] = f"{email['from_name']} <{email['from_email']}>"
    msg["Subject"] = email["subject"]
    msg[SEED_LABEL] = "true"

    # Preserve the original timestamp from the dummy data
    try:
        dt = parsedate_to_datetime(email["timestamp"]) if "T" not in email["timestamp"] \
            else datetime.fromisoformat(email["timestamp"])
        msg["Date"] = format_datetime(dt)
        internal_date_ms = int(dt.timestamp() * 1000)
    except Exception:
        dt = datetime.now(timezone.utc)
        msg["Date"] = format_datetime(dt)
        internal_date_ms = int(dt.timestamp() * 1000)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return raw, internal_date_ms


def insert_email(service, raw: str, internal_date_ms: int) -> str:
    result = service.users().messages().insert(
        userId="me",
        internalDateSource="dateHeader",
        body={
            "raw": raw,
            "labelIds": ["INBOX", "UNREAD"],
        },
    ).execute()
    return result["id"]


def list_seeded_messages(service) -> list[str]:
    """Return message IDs that were inserted by this script."""
    result = service.users().messages().list(
        userId="me",
        q=f"label:INBOX",
        maxResults=200,
    ).execute()
    messages = result.get("messages", [])

    seeded_ids = []
    for m in messages:
        detail = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=[SEED_LABEL]
        ).execute()
        headers = detail.get("payload", {}).get("headers", [])
        for h in headers:
            if h["name"] == SEED_LABEL and h["value"] == "true":
                seeded_ids.append(m["id"])
                break
    return seeded_ids


def clear_seeded(service):
    print("Finding seeded messages...")
    ids = list_seeded_messages(service)
    if not ids:
        print("No seeded messages found.")
        return
    print(f"Deleting {len(ids)} seeded message(s)...")
    for mid in ids:
        service.users().messages().trash(userId="me", id=mid).execute()
        print(f"  Trashed {mid}")
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Seed propmind.test@gmail.com with dummy tenant emails")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't insert")
    parser.add_argument("--clear", action="store_true", help="Delete previously seeded messages")
    args = parser.parse_args()

    to_address = os.environ.get("GMAIL_ADDRESS", "propmind.test@gmail.com")

    with open(DUMMY_DATA) as f:
        emails = json.load(f)

    if args.dry_run:
        print(f"DRY RUN — would insert {len(emails)} emails into {to_address}\n")
        for i, e in enumerate(emails, 1):
            print(f"  {i:2}. [{e.get('urgency','?'):6}] {e['from_name']:<25} — {e['subject'][:50]}")
        return

    service = get_service()

    if args.clear:
        clear_seeded(service)
        return

    print(f"Inserting {len(emails)} dummy emails into {to_address}...\n")
    inserted = 0
    for i, email in enumerate(emails, 1):
        try:
            raw, ts_ms = build_raw_message(email, to_address)
            msg_id = insert_email(service, raw, ts_ms)
            print(f"  {i:2}. ✓ [{email.get('urgency','?'):6}] {email['from_name']:<25} — {email['subject'][:45]}")
            inserted += 1
            time.sleep(0.3)  # stay well under Gmail API rate limits
        except Exception as exc:
            print(f"  {i:2}. ✗ FAILED {email['subject'][:45]} — {exc}")

    print(f"\n{inserted}/{len(emails)} emails inserted into {to_address}.")
    print("The polling pipeline will pick them up within 5 minutes,")
    print("or call POST /api/trigger-poll to process them immediately.")


if __name__ == "__main__":
    main()
