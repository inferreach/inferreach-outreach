"""
InferReach Outreach Engine
Send curious, non-salesy cold emails + 3-day follow-ups via Resend.

Commands:
    python outreach.py import      # Load prospects.csv into DB
    python outreach.py send        # Send initial emails
    python outreach.py followup    # Send 3-day follow-ups
    python outreach.py status      # Show stats
"""

import csv
import sqlite3
import argparse
import time
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import resend
except ImportError:
    print("❌ Missing 'resend' package. Run: pip install resend")
    sys.exit(1)

# ============================================================
# CONFIG — EDIT THESE
# ============================================================

CONFIG = {
    "resend_api_key": os.environ.get("RESEND_API_KEY", ""),
    "sender_email": "hello@tryinferreach.com",
    "sender_name": "Prajwol",
    "reply_to": "hello@tryinferreach.com",
    "signature": """Cheers,
Prajwol
inferreach.com""",
    "db_path": "outreach.db",
    "csv_path": "prospects.csv",
    "delay_seconds": 2,
}

if not CONFIG["resend_api_key"]:
    print("❌ RESEND_API_KEY environment variable not set.")
    print('   Run: $env:RESEND_API_KEY="re_your_actual_key_here"')
    sys.exit(1)

resend.api_key = CONFIG["resend_api_key"]

# ============================================================
# EMAIL TEMPLATES — curious, no pitch
# ============================================================

INITIAL_SUBJECT = "Quick question about {company}"

INITIAL_BODY = """Hi {first_name},

{saw_your_company_line}

{pain_line}

I'm researching how GTM teams at companies like yours handle this — not selling anything, just trying to understand the space better.

Would you be open to a 15-min chat sometime? Happy to share what I'm learning from others in the same boat.

{signature}"""

FOLLOWUP_SUBJECT = "Re: Quick question about {company}"

FOLLOWUP_BODY = """Hi {first_name},

Just bumping this in case it got buried.

No agenda — genuinely just curious how you're handling this.

If now isn't a good time, no worries at all. Just reply "not now" and I'll leave you alone.

{signature}"""


def build_initial(prospect):
    """Build a curious, non-salesy initial email."""
    company = prospect.get("company") or "your team"
    first_name = prospect.get("first_name") or "there"
    role = prospect.get("role") or "your team"
    pain = prospect.get("pain_point") or ""
    news = prospect.get("recent_news") or ""

    # Opening line — shows we did homework
    if news:
        saw_line = f"Congrats on the {news.lower()} — really impressive."
    else:
        saw_line = f"Came across {company} while looking into GTM data stacks at companies like yours."

    # Curious question about their world
    if pain:
        pain_line = f'I keep hearing teams at your stage run into "{pain.lower()}" — curious if that\'s something you\'re dealing with too?'
    else:
        pain_line = "I've been talking to a lot of teams about how they keep their CRM data clean and their pipeline healthy. Curious how you're approaching it at " + company + "."

    return {
        "subject": INITIAL_SUBJECT.format(company=company),
        "body": INITIAL_BODY.format(
            first_name=first_name,
            company=company,
            role=role,
            saw_your_company_line=saw_line,
            pain_line=pain_line,
            signature=CONFIG["signature"],
        ),
    }


def build_followup(prospect):
    """Build a soft, curious follow-up."""
    company = prospect.get("company") or "your team"
    first_name = prospect.get("first_name") or "there"
    return {
        "subject": FOLLOWUP_SUBJECT.format(company=company),
        "body": FOLLOWUP_BODY.format(
            first_name=first_name,
            signature=CONFIG["signature"],
        ),
    }


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(CONFIG["db_path"])
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            first_name TEXT,
            company TEXT,
            role TEXT,
            pain_point TEXT,
            recent_news TEXT,
            status TEXT DEFAULT 'pending',
            sent_at TEXT,
            followed_up_at TEXT,
            replied_at TEXT,
            error TEXT
        )
    """)
    conn.commit()
    return conn


# ============================================================
# SEND HELPER
# ============================================================

def send_email(to_email, subject, body):
    try:
        result = resend.Emails.send({
            "from": f"{CONFIG['sender_name']} <{CONFIG['sender_email']}>",
            "to": [to_email],
            "reply_to": CONFIG["reply_to"],
            "subject": subject,
            "text": body,
        })
        return result.get("id")
    except Exception as e:
        print(f"  ✗ Send failed: {e}")
        return None


# ============================================================
# COMMANDS
# ============================================================

def cmd_import(args):
    if not Path(CONFIG["csv_path"]).exists():
        print(f"❌ {CONFIG['csv_path']} not found.")
        print("   Create it with columns: email,first_name,company,role,pain_point,recent_news")
        return

    conn = init_db()
    c = conn.cursor()
    added, skipped, invalid = 0, 0, 0

    with open(CONFIG["csv_path"], encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = (row.get("email") or "").strip().lower()
            if not email or "@" not in email:
                invalid += 1
                continue
            try:
                c.execute("""
                    INSERT INTO prospects
                    (email, first_name, company, role, pain_point, recent_news)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    email,
                    (row.get("first_name") or "").strip(),
                    (row.get("company") or "").strip(),
                    (row.get("role") or "").strip(),
                    (row.get("pain_point") or "").strip(),
                    (row.get("recent_news") or "").strip(),
                ))
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1

    conn.commit()
    conn.close()
    print(f"✅ Imported {added} new prospects.")
    if skipped:
        print(f"   Skipped {skipped} duplicates.")
    if invalid:
        print(f"   Skipped {invalid} invalid emails.")


def cmd_send(args):
    limit = args.limit or 10

    conn = init_db()
    c = conn.cursor()
    c.execute("""
        SELECT id, email, first_name, company, role, pain_point, recent_news
        FROM prospects WHERE status = 'pending' LIMIT ?
    """, (limit,))
    rows = c.fetchall()

    if not rows:
        print("No pending prospects.")
        return

    print(f"📧 Sending curious emails to {len(rows)} prospects...\n")

    success, failed = 0, 0
    for i, row in enumerate(rows):
        pid, email, first_name, company, role, pain, news = row
        prospect = {
            "first_name": first_name, "company": company, "role": role,
            "pain_point": pain, "recent_news": news,
        }
        email_data = build_initial(prospect)
        msg_id = send_email(email, email_data["subject"], email_data["body"])

        if msg_id:
            c.execute("""
                UPDATE prospects SET status = 'sent', sent_at = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), pid))
            conn.commit()
            print(f"  ✓ {i+1}/{len(rows)} → {email}")
            success += 1
        else:
            c.execute("""
                UPDATE prospects SET status = 'failed', error = 'send_failed'
                WHERE id = ?
            """, (pid,))
            conn.commit()
            print(f"  ✗ {i+1}/{len(rows)} failed → {email}")
            failed += 1

        if i < len(rows) - 1:
            time.sleep(CONFIG["delay_seconds"])

    conn.close()
    print(f"\n✅ Done. Sent: {success}, Failed: {failed}")


def cmd_followup(args):
    conn = init_db()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()

    c.execute("""
        SELECT id, email, first_name, company
        FROM prospects
        WHERE status = 'sent' AND sent_at <= ? AND replied_at IS NULL
    """, (cutoff,))
    rows = c.fetchall()

    if not rows:
        print("No follow-ups due.")
        conn.close()
        return

    print(f"📧 Sending soft follow-ups to {len(rows)} prospects...\n")

    success, failed = 0, 0
    for i, (pid, email, first_name, company) in enumerate(rows):
        prospect = {"first_name": first_name, "company": company}
        email_data = build_followup(prospect)
        msg_id = send_email(email, email_data["subject"], email_data["body"])

        if msg_id:
            c.execute("""
                UPDATE prospects SET status = 'followed_up', followed_up_at = ?
                WHERE id = ?
            """, (datetime.now().isoformat(), pid))
            conn.commit()
            print(f"  ✓ {i+1}/{len(rows)} → {email}")
            success += 1
        else:
            print(f"  ✗ {i+1}/{len(rows)} failed → {email}")
            failed += 1

        if i < len(rows) - 1:
            time.sleep(CONFIG["delay_seconds"])

    conn.close()
    print(f"\n✅ Done. Sent: {success}, Failed: {failed}")


def cmd_status(args):
    conn = init_db()
    c = conn.cursor()
    c.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status")
    rows = c.fetchall()

    print("\n📊 Outreach Status")
    print("─" * 35)
    total = 0
    for status, count in rows:
        print(f"  {status:<15} {count}")
        total += count
    print("─" * 35)
    print(f"  {'TOTAL':<15} {total}")

    c.execute("SELECT COUNT(*) FROM prospects WHERE replied_at IS NOT NULL")
    replies = c.fetchone()[0]
    if total > 0:
        rate = (replies / total) * 100
        print(f"\n  Reply rate: {rate:.1f}%")

    conn.close()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="InferReach Outreach Engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("import", help="Load prospects.csv into DB")

    send_parser = subparsers.add_parser("send", help="Send initial emails")
    send_parser.add_argument("--limit", type=int, default=10, help="Max emails to send")

    subparsers.add_parser("followup", help="Send 3-day follow-ups")
    subparsers.add_parser("status", help="Show stats")

    args = parser.parse_args()

    commands = {
        "import": cmd_import,
        "send": cmd_send,
        "followup": cmd_followup,
        "status": cmd_status,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()