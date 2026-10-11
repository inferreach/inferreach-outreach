"""
InferReach Outreach Engine
Send curious, non-salesy cold emails + 3-day follow-ups via Resend.

Commands:
    python outreach.py preview              # Preview emails without sending
    python outreach.py import               # Load prospects.csv into DB (--campaign, --file)
    python outreach.py send                 # Send initial emails (--limit, --campaign)
    python outreach.py followup             # Send 3-day follow-ups (--limit, --campaign)
    python outreach.py mark-replied <email> # Record reply sentiment and notes
    python outreach.py opt-out <target>     # Add email or domain to suppression list
    python outreach.py history <email>      # View dispatch history for a prospect
    python outreach.py suppression          # View suppression / DNC list
    python outreach.py status               # Show comprehensive outreach metrics
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
    print("Missing 'resend' package. Run: pip install resend")
    sys.exit(1)

# ============================================================
# CONFIG
# ============================================================

CONFIG = {
    "resend_api_key": os.environ.get("RESEND_API_KEY", ""),
    # UPDATED: Must match the domain verified in Resend (e.g., send.tryinferreach.com)
    "sender_email": "hello@send.tryinferreach.com",
    "sender_name": "Prajwol",
    # KEPT: Routes replies to your Zoho Mail inbox
    "reply_to": "hello@tryinferreach.com",
    "signature": """Cheers,
Prajwol
inferreach.com""",
    "db_path": "outreach.db",
    "csv_path": "prospects.csv",
    "delay_seconds": 2,
}

# ============================================================
# EMAIL TEMPLATES
# ============================================================

INITIAL_SUBJECT = "Quick question about {company}"

INITIAL_BODY = """Hi {first_name},

{saw_your_company_line}

{pain_line}

I'm researching how GTM teams at companies like yours handle this. Not selling anything, just trying to understand the space better.

Would you be open to a 15-min chat sometime? Happy to share what I'm learning from others in the same boat.

{signature}"""

FOLLOWUP_SUBJECT = "Re: Quick question about {company}"

FOLLOWUP_BODY = """Hi {first_name},

Just bumping this in case it got buried.

No agenda, genuinely just curious how you're handling this.

If now isn't a good time, no worries at all. Just reply "not now" and I'll leave you alone.

{signature}"""


def build_initial(prospect):
    """Build a curious, non-salesy initial email."""
    company = prospect.get("company") or "your team"
    first_name = prospect.get("first_name") or "there"
    role = prospect.get("role") or "your team"
    pain = prospect.get("pain_point") or ""
    news = prospect.get("recent_news") or ""

    # Opening line, shows we did homework
    if news:
        saw_line = f"Congrats on {news}. Really impressive."
    else:
        saw_line = f"Came across {company} while looking into GTM data stacks at companies like yours."

    # Curious question about their world
    if pain:
        pain_line = f"{pain} Curious if that is something you are dealing with at {company} too."
    else:
        pain_line = f"I have been talking to a lot of teams about how they keep their CRM data clean and their pipeline healthy. Curious how you are approaching it at {company}."

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
# DATABASE & MIGRATIONS
# ============================================================

def init_db():
    conn = sqlite3.connect(CONFIG["db_path"])
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    c = conn.cursor()

    # 1. Base prospects table
    c.execute("""
        CREATE TABLE IF NOT EXISTS prospects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL COLLATE NOCASE,
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

    # 2. Add upgraded columns if they do not exist
    c.execute("PRAGMA table_info(prospects)")
    existing_cols = {row[1] for row in c.fetchall()}
    new_cols = [
        ("campaign", "TEXT DEFAULT 'default'"),
        ("sentiment", "TEXT"),
        ("notes", "TEXT"),
        ("created_at", "TEXT"),
        ("updated_at", "TEXT"),
    ]
    for col_name, col_def in new_cols:
        if col_name not in existing_cols:
            c.execute(f"ALTER TABLE prospects ADD COLUMN {col_name} {col_def}")

    # 3. Message outbox and audit log table
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
            step_number INTEGER DEFAULT 1,
            step_type TEXT DEFAULT 'initial',
            resend_id TEXT,
            subject TEXT,
            body TEXT,
            status TEXT DEFAULT 'sent',
            sent_at TEXT,
            error TEXT
        )
    """)

    # 4. Suppression / Do-Not-Contact list
    c.execute("""
        CREATE TABLE IF NOT EXISTS suppression_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE COLLATE NOCASE,
            domain TEXT COLLATE NOCASE,
            reason TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 5. Strategic indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_prospects_status ON prospects(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_prospects_email ON prospects(email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_prospects_campaign ON prospects(campaign)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_prospects_followup ON prospects(status, sent_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_prospect ON messages(prospect_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_resend ON messages(resend_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_suppression_email ON suppression_list(email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_suppression_domain ON suppression_list(domain)")

    # 6. Automatic backfill: Migrate historical sent records into messages table if empty
    c.execute("SELECT COUNT(*) FROM messages")
    if c.fetchone()[0] == 0:
        c.execute("""
            SELECT id, company, sent_at, followed_up_at
            FROM prospects
            WHERE sent_at IS NOT NULL
        """)
        for pid, comp, s_at, f_at in c.fetchall():
            c.execute("""
                INSERT INTO messages (prospect_id, step_number, step_type, subject, status, sent_at)
                VALUES (?, 1, 'initial', ?, 'sent', ?)
            """, (pid, f"Quick question about {comp or 'your team'}", s_at))
            if f_at:
                c.execute("""
                    INSERT INTO messages (prospect_id, step_number, step_type, subject, status, sent_at)
                    VALUES (?, 2, 'followup', ?, 'sent', ?)
                """, (pid, f"Re: Quick question about {comp or 'your team'}", f_at))

    conn.commit()
    return conn


def is_suppressed(conn, email):
    """Check if an email or its domain is on the suppression list."""
    if not email:
        return False, None
    clean_email = email.strip().lower()
    domain = clean_email.split("@")[1] if "@" in clean_email else ""
    c = conn.cursor()
    c.execute("""
        SELECT reason FROM suppression_list
        WHERE email = ? OR (domain = ? AND domain != '')
        LIMIT 1
    """, (clean_email, domain))
    row = c.fetchone()
    if row:
        return True, row[0]
    return False, None


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
        print(f"  Send failed: {e}")
        return None


# ============================================================
# COMMANDS
# ============================================================

def cmd_preview(args):
    """Preview the emails without sending. Reads from CSV directly."""
    csv_file = getattr(args, "file", None) or CONFIG["csv_path"]
    if not Path(csv_file).exists():
        print(f"{csv_file} not found.")
        return

    with open(csv_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        print("CSV is empty.")
        return

    for i, row in enumerate(rows):
        email_data = build_initial(row)
        print("=" * 60)
        print(f"TO:      {row.get('email', '')}")
        print(f"SUBJECT: {email_data['subject']}")
        print("-" * 60)
        print(email_data["body"])
        print()

    print("=" * 60)
    print(f"{len(rows)} email(s) previewed. Nothing was sent.")


def cmd_import(args):
    csv_file = getattr(args, "file", None) or CONFIG["csv_path"]
    campaign = getattr(args, "campaign", None) or "default"

    if not Path(csv_file).exists():
        print(f"{csv_file} not found.")
        print("Create it with columns: email,first_name,company,role,pain_point,recent_news")
        return

    conn = init_db()
    c = conn.cursor()
    added, skipped, invalid, suppressed = 0, 0, 0, 0
    now_iso = datetime.now().isoformat()

    with open(csv_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = (row.get("email") or "").strip().lower()
            if not email or "@" not in email:
                invalid += 1
                continue

            suppressed_flag, reason = is_suppressed(conn, email)
            if suppressed_flag:
                suppressed += 1
                continue

            try:
                c.execute("""
                    INSERT INTO prospects
                    (email, first_name, company, role, pain_point, recent_news, campaign, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    email,
                    (row.get("first_name") or "").strip(),
                    (row.get("company") or "").strip(),
                    (row.get("role") or "").strip(),
                    (row.get("pain_point") or "").strip(),
                    (row.get("recent_news") or "").strip(),
                    campaign,
                    now_iso,
                    now_iso,
                ))
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1

    conn.commit()
    conn.close()
    print(f"Imported {added} new prospects (campaign: '{campaign}').")
    if skipped:
        print(f"Skipped {skipped} duplicates.")
    if invalid:
        print(f"Skipped {invalid} invalid emails.")
    if suppressed:
        print(f"Skipped {suppressed} suppressed/opted-out emails.")


def cmd_send(args):
    if not CONFIG["resend_api_key"]:
        print("RESEND_API_KEY environment variable not set.")
        print('Run: $env:RESEND_API_KEY="re_your_actual_key_here"')
        return

    resend.api_key = CONFIG["resend_api_key"]
    limit = getattr(args, "limit", None) or 10
    campaign = getattr(args, "campaign", None)

    conn = init_db()
    c = conn.cursor()

    query = """
        SELECT id, email, first_name, company, role, pain_point, recent_news
        FROM prospects
        WHERE status = 'pending'
    """
    params = []
    if campaign:
        query += " AND campaign = ?"
        params.append(campaign)
    query += " LIMIT ?"
    params.append(limit)

    c.execute(query, tuple(params))
    rows = c.fetchall()

    if not rows:
        print(f"No pending prospects{' for campaign: ' + campaign if campaign else ''}.")
        conn.close()
        return

    print(f"Sending curious emails to {len(rows)} prospects...\n")

    success, failed, skipped_dnc = 0, 0, 0
    for i, row in enumerate(rows):
        pid, email, first_name, company, role, pain, news = row

        suppressed_flag, reason = is_suppressed(conn, email)
        if suppressed_flag:
            now_iso = datetime.now().isoformat()
            c.execute("""
                UPDATE prospects SET status = 'opted_out', updated_at = ?
                WHERE id = ?
            """, (now_iso, pid))
            conn.commit()
            print(f"  {i+1}/{len(rows)} SKIPPED (Suppressed: {reason}) -> {email}")
            skipped_dnc += 1
            continue

        prospect = {
            "first_name": first_name, "company": company, "role": role,
            "pain_point": pain, "recent_news": news,
        }
        email_data = build_initial(prospect)
        msg_id = send_email(email, email_data["subject"], email_data["body"])
        now_iso = datetime.now().isoformat()

        if msg_id:
            c.execute("""
                INSERT INTO messages (prospect_id, step_number, step_type, resend_id, subject, body, status, sent_at)
                VALUES (?, 1, 'initial', ?, ?, ?, 'sent', ?)
            """, (pid, msg_id, email_data["subject"], email_data["body"], now_iso))
            c.execute("""
                UPDATE prospects SET status = 'sent', sent_at = ?, updated_at = ?
                WHERE id = ?
            """, (now_iso, now_iso, pid))
            conn.commit()
            print(f"  {i+1}/{len(rows)} sent -> {email} (id: {msg_id})")
            success += 1
        else:
            c.execute("""
                INSERT INTO messages (prospect_id, step_number, step_type, subject, body, status, sent_at, error)
                VALUES (?, 1, 'initial', ?, ?, 'failed', ?, 'send_failed')
            """, (pid, email_data["subject"], email_data["body"], now_iso))
            c.execute("""
                UPDATE prospects SET status = 'failed', error = 'send_failed', updated_at = ?
                WHERE id = ?
            """, (now_iso, pid))
            conn.commit()
            print(f"  {i+1}/{len(rows)} FAILED -> {email}")
            failed += 1

        if i < len(rows) - 1:
            time.sleep(CONFIG["delay_seconds"])

    conn.close()
    summary = f"\nDone. Sent: {success}, Failed: {failed}"
    if skipped_dnc:
        summary += f", Suppressed: {skipped_dnc}"
    print(summary)


def cmd_followup(args):
    if not CONFIG["resend_api_key"]:
        print("RESEND_API_KEY environment variable not set.")
        return

    resend.api_key = CONFIG["resend_api_key"]
    limit = getattr(args, "limit", None) or 10
    campaign = getattr(args, "campaign", None)

    conn = init_db()
    c = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=3)).isoformat()

    query = """
        SELECT id, email, first_name, company
        FROM prospects
        WHERE status = 'sent' AND sent_at <= ? AND replied_at IS NULL
    """
    params = [cutoff]
    if campaign:
        query += " AND campaign = ?"
        params.append(campaign)
    query += " LIMIT ?"
    params.append(limit)

    c.execute(query, tuple(params))
    rows = c.fetchall()

    if not rows:
        print("No follow-ups due.")
        conn.close()
        return

    print(f"Sending soft follow-ups to {len(rows)} prospects...\n")

    success, failed, skipped_dnc = 0, 0, 0
    for i, (pid, email, first_name, company) in enumerate(rows):
        suppressed_flag, reason = is_suppressed(conn, email)
        if suppressed_flag:
            now_iso = datetime.now().isoformat()
            c.execute("""
                UPDATE prospects SET status = 'opted_out', updated_at = ?
                WHERE id = ?
            """, (now_iso, pid))
            conn.commit()
            print(f"  {i+1}/{len(rows)} SKIPPED (Suppressed: {reason}) -> {email}")
            skipped_dnc += 1
            continue

        prospect = {"first_name": first_name, "company": company}
        email_data = build_followup(prospect)
        msg_id = send_email(email, email_data["subject"], email_data["body"])
        now_iso = datetime.now().isoformat()

        if msg_id:
            c.execute("""
                INSERT INTO messages (prospect_id, step_number, step_type, resend_id, subject, body, status, sent_at)
                VALUES (?, 2, 'followup', ?, ?, ?, 'sent', ?)
            """, (pid, msg_id, email_data["subject"], email_data["body"], now_iso))
            c.execute("""
                UPDATE prospects SET status = 'followed_up', followed_up_at = ?, updated_at = ?
                WHERE id = ?
            """, (now_iso, now_iso, pid))
            conn.commit()
            print(f"  {i+1}/{len(rows)} sent -> {email} (id: {msg_id})")
            success += 1
        else:
            c.execute("""
                INSERT INTO messages (prospect_id, step_number, step_type, subject, body, status, sent_at, error)
                VALUES (?, 2, 'followup', ?, ?, 'failed', ?, 'followup_failed')
            """, (pid, email_data["subject"], email_data["body"], now_iso))
            c.execute("""
                UPDATE prospects SET error = 'followup_failed', updated_at = ?
                WHERE id = ?
            """, (now_iso, pid))
            conn.commit()
            print(f"  {i+1}/{len(rows)} FAILED -> {email}")
            failed += 1

        if i < len(rows) - 1:
            time.sleep(CONFIG["delay_seconds"])

    conn.close()
    summary = f"\nDone. Sent: {success}, Failed: {failed}"
    if skipped_dnc:
        summary += f", Suppressed: {skipped_dnc}"
    print(summary)


def cmd_mark_replied(args):
    """Record a reply from a prospect with optional sentiment and notes."""
    email = args.email.strip().lower()
    sentiment = args.sentiment
    notes = args.notes or ""

    conn = init_db()
    c = conn.cursor()
    c.execute("SELECT id, first_name, company FROM prospects WHERE email = ?", (email,))
    row = c.fetchone()
    if not row:
        print(f"Prospect not found: {email}")
        conn.close()
        return

    pid, first_name, company = row
    now_iso = datetime.now().isoformat()

    c.execute("""
        UPDATE prospects
        SET status = 'replied', replied_at = ?, sentiment = ?, notes = ?, updated_at = ?
        WHERE id = ?
    """, (now_iso, sentiment, notes, now_iso, pid))
    conn.commit()
    conn.close()

    print(f"Marked {email} ({first_name} @ {company}) as REPLIED.")
    if sentiment:
        print(f"  Sentiment: {sentiment}")
    if notes:
        print(f"  Notes: {notes}")


def cmd_opt_out(args):
    """Add an email or entire domain to the suppression list."""
    target = args.target.strip().lower()
    reason = args.reason or "opt_out"
    now_iso = datetime.now().isoformat()

    conn = init_db()
    c = conn.cursor()

    if target.startswith("@") or ("@" not in target and "." in target):
        domain = target.lstrip("@")
        try:
            c.execute("""
                INSERT INTO suppression_list (domain, reason)
                VALUES (?, ?)
            """, (domain, reason))
        except sqlite3.IntegrityError:
            print(f"Domain '{domain}' is already suppressed.")

        c.execute("""
            UPDATE prospects SET status = 'opted_out', updated_at = ?
            WHERE email LIKE ?
        """, (now_iso, f"%@{domain}"))
        affected = c.rowcount
        conn.commit()
        print(f"Domain '@{domain}' added to suppression list ({reason}).")
        if affected:
            print(f"Updated {affected} existing prospects to 'opted_out'.")
    else:
        email = target
        domain = email.split("@")[1] if "@" in email else ""
        try:
            c.execute("""
                INSERT INTO suppression_list (email, domain, reason)
                VALUES (?, ?, ?)
            """, (email, domain, reason))
        except sqlite3.IntegrityError:
            print(f"Email '{email}' is already suppressed.")

        c.execute("""
            UPDATE prospects SET status = 'opted_out', updated_at = ?
            WHERE email = ?
        """, (now_iso, email))
        affected = c.rowcount
        conn.commit()
        print(f"Email '{email}' added to suppression list ({reason}).")
        if affected:
            print("Updated prospect status to 'opted_out'.")

    conn.close()


def cmd_suppression(args):
    """Display the suppression / Do-Not-Contact list."""
    conn = init_db()
    c = conn.cursor()
    c.execute("SELECT id, email, domain, reason, created_at FROM suppression_list ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    if not rows:
        print("Suppression list is empty.")
        return

    print("\nSuppression List (DNC / Opt-Outs / Bounces)")
    print("-" * 65)
    for sid, email, domain, reason, created_at in rows:
        target = email if email else f"@{domain}"
        print(f"  {target:<32} {reason:<15} {created_at}")
    print("-" * 65)
    print(f"Total: {len(rows)} suppressed target(s)\n")


def cmd_history(args):
    """View communication history and Resend message IDs for a prospect."""
    email = args.email.strip().lower()
    conn = init_db()
    c = conn.cursor()

    c.execute("""
        SELECT id, first_name, company, role, campaign, status, sentiment, notes, created_at
        FROM prospects WHERE email = ?
    """, (email,))
    row = c.fetchone()
    if not row:
        print(f"Prospect not found: {email}")
        conn.close()
        return

    pid, first_name, company, role, campaign, status, sentiment, notes, created_at = row
    print("=" * 60)
    print(f"PROSPECT:   {email}")
    print(f"NAME:       {first_name} | ROLE: {role} | COMPANY: {company}")
    print(f"CAMPAIGN:   {campaign} | STATUS: {status}")
    if sentiment:
        print(f"SENTIMENT:  {sentiment}")
    if notes:
        print(f"NOTES:      {notes}")
    print("-" * 60)

    c.execute("""
        SELECT step_number, step_type, resend_id, subject, sent_at, status, error
        FROM messages WHERE prospect_id = ?
        ORDER BY id ASC
    """, (pid,))
    msgs = c.fetchall()
    if not msgs:
        print("No messages recorded in outbox.")
    else:
        print(f"TOUCHPOINTS ({len(msgs)} total):")
        for step_num, step_type, resend_id, subject, sent_at, m_status, error in msgs:
            print(f"  Step {step_num} ({step_type}) -> [{m_status.upper()}] at {sent_at}")
            print(f"    Subject:   {subject}")
            if resend_id:
                print(f"    Resend ID: {resend_id}")
            if error:
                print(f"    Error:     {error}")
    print("=" * 60)
    conn.close()


def cmd_status(args):
    """Display comprehensive status breakdown of the outreach pipeline."""
    conn = init_db()
    c = conn.cursor()

    c.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status")
    status_counts = dict(c.fetchall())

    c.execute("SELECT campaign, COUNT(*) FROM prospects GROUP BY campaign")
    campaign_counts = c.fetchall()

    c.execute("""
        SELECT sentiment, COUNT(*) FROM prospects
        WHERE status = 'replied' AND sentiment IS NOT NULL
        GROUP BY sentiment
    """)
    sentiment_counts = c.fetchall()

    c.execute("SELECT step_type, status, COUNT(*) FROM messages GROUP BY step_type, status")
    message_counts = c.fetchall()

    c.execute("SELECT COUNT(*) FROM suppression_list")
    suppression_count = c.fetchone()[0]

    total_prospects = sum(status_counts.values())

    print("\nOutreach Pipeline Status")
    print("=" * 45)
    print("  Prospect Status:")
    for st in ["pending", "sent", "followed_up", "replied", "opted_out", "failed"]:
        if st in status_counts:
            print(f"    {st:<16} {status_counts[st]}")
    for st, count in status_counts.items():
        if st not in ["pending", "sent", "followed_up", "replied", "opted_out", "failed"]:
            print(f"    {st:<16} {count}")
    print("  " + "-" * 40)
    print(f"    {'TOTAL PROSPECTS':<16} {total_prospects}")

    replies = status_counts.get("replied", 0)
    if total_prospects > 0:
        reply_rate = (replies / total_prospects) * 100
        print(f"    {'REPLY RATE':<16} {reply_rate:.1f}%")

    if sentiment_counts:
        print("\n  Reply Sentiments:")
        for sent, cnt in sentiment_counts:
            print(f"    {sent:<16} {cnt}")

    if len(campaign_counts) > 1 or (campaign_counts and campaign_counts[0][0] != "default"):
        print("\n  Campaigns:")
        for camp, cnt in campaign_counts:
            print(f"    {(camp or 'default'):<16} {cnt}")

    if message_counts:
        print("\n  Dispatched Messages:")
        for stype, mstat, cnt in message_counts:
            print(f"    {stype} ({mstat}): {cnt}")

    if suppression_count > 0:
        print(f"\n  Suppressed Contacts: {suppression_count}")

    print("=" * 45 + "\n")
    conn.close()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="InferReach Outreach Engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # preview
    prev_parser = subparsers.add_parser("preview", help="Preview emails without sending")
    prev_parser.add_argument("--file", type=str, help="Custom CSV file path")

    # import
    import_parser = subparsers.add_parser("import", help="Load prospects.csv into DB")
    import_parser.add_argument("--file", type=str, help="Custom CSV file path")
    import_parser.add_argument("--campaign", type=str, default="default", help="Campaign/batch tag")

    # send
    send_parser = subparsers.add_parser("send", help="Send initial emails")
    send_parser.add_argument("--limit", type=int, default=10, help="Max emails to send")
    send_parser.add_argument("--campaign", type=str, help="Filter by campaign tag")

    # followup
    followup_parser = subparsers.add_parser("followup", help="Send 3-day follow-ups")
    followup_parser.add_argument("--limit", type=int, default=10, help="Max follow-ups to send")
    followup_parser.add_argument("--campaign", type=str, help="Filter by campaign tag")

    # status
    subparsers.add_parser("status", help="Show pipeline and outreach stats")

    # mark-replied
    replied_parser = subparsers.add_parser("mark-replied", help="Record a prospect reply")
    replied_parser.add_argument("email", type=str, help="Prospect email")
    replied_parser.add_argument("--sentiment", type=str, choices=["interested", "not_interested", "not_now", "wrong_person", "booked"], help="Sentiment/outcome")
    replied_parser.add_argument("--notes", type=str, help="Notes about the reply")

    # opt-out
    optout_parser = subparsers.add_parser("opt-out", help="Add email or @domain to suppression list")
    optout_parser.add_argument("target", type=str, help="Email (e.g. tom@acme.com) or domain (e.g. @acme.com)")
    optout_parser.add_argument("--reason", type=str, default="opt_out", help="Reason (opt_out, bounced, manual)")

    # history
    history_parser = subparsers.add_parser("history", help="View communication history for a prospect")
    history_parser.add_argument("email", type=str, help="Prospect email")

    # suppression
    subparsers.add_parser("suppression", help="View suppression / DNC list")

    args = parser.parse_args()

    commands = {
        "preview": cmd_preview,
        "import": cmd_import,
        "send": cmd_send,
        "followup": cmd_followup,
        "status": cmd_status,
        "mark-replied": cmd_mark_replied,
        "opt-out": cmd_opt_out,
        "history": cmd_history,
        "suppression": cmd_suppression,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()