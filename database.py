import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "emails.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                gmail_message_id TEXT UNIQUE NOT NULL,
                sender_name TEXT,
                sender_email TEXT,
                subject TEXT,
                body TEXT,
                received_at TIMESTAMP,
                category TEXT,
                urgency TEXT,
                draft_response TEXT,
                confidence_score INTEGER,
                status TEXT DEFAULT 'PENDING',
                processed_at TIMESTAMP,
                property_hints TEXT DEFAULT '',
                property_match_status TEXT DEFAULT 'UNMATCHED',
                matched_property TEXT DEFAULT ''
            )
        """)
        # Safe migration for existing tables that predate these columns
        for col, default in [
            ("property_hints", "''"),
            ("property_match_status", "'UNMATCHED'"),
            ("matched_property", "''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE emails ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass  # column already exists
        conn.commit()


def insert_email(record: dict) -> int:
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO emails
                (tenant_id, gmail_message_id, sender_name, sender_email,
                 subject, body, received_at, category, urgency,
                 draft_response, confidence_score, status, processed_at,
                 property_hints, property_match_status, matched_property)
            VALUES
                (:tenant_id, :gmail_message_id, :sender_name, :sender_email,
                 :subject, :body, :received_at, :category, :urgency,
                 :draft_response, :confidence_score, :status, :processed_at,
                 :property_hints, :property_match_status, :matched_property)
            """,
            record,
        )
        conn.commit()
        return cursor.lastrowid


def get_emails(tenant_id: str, status: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM emails WHERE tenant_id=? AND status=? ORDER BY received_at DESC",
                (tenant_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM emails WHERE tenant_id=? ORDER BY received_at DESC",
                (tenant_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_email_by_id(email_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
        return dict(row) if row else None


def update_status(email_id: int, status: str) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE emails SET status=? WHERE id=?",
            (status, email_id),
        )
        conn.commit()
    return get_email_by_id(email_id)


def email_exists(gmail_message_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM emails WHERE gmail_message_id=?",
            (gmail_message_id,),
        ).fetchone()
        return row is not None


def count_emails(tenant_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        return row[0]


def match_property(email_id: int, matched_property: str) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE emails SET matched_property=?, property_match_status='MATCHED' WHERE id=?",
            (matched_property, email_id),
        )
        conn.commit()
    return get_email_by_id(email_id)


def get_all_gmail_ids() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT gmail_message_id FROM emails").fetchall()
        return [r[0] for r in rows]


def clear_all_emails():
    with get_conn() as conn:
        conn.execute("DELETE FROM emails")
        conn.commit()
