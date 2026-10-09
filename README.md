# InferReach Outreach Engine

> **Company:** InferReach ([inferreach.com](https://inferreach.com))  
> **Sender:** Prajwol (`hello@tryinferreach.com`)  
> **Purpose:** Engine for personalized, curious, non-salesy cold outreach and automated follow-ups via Resend.

---

## Overview

This repository contains the codebase, database, and prospect data for **InferReach's outbound outreach engine**. It is designed to handle research-backed B2B cold outreach without feeling spammy or salesy.

### Core Workflow
1. **Source & Structure:** Prospect profiles and research are staged in `prospects.csv`.
2. **Import:** Loaded into local SQLite database (`outreach.db`).
3. **Initial Touchpoint:** Send personalized, research-driven email via [Resend](https://resend.com).
4. **Follow-Up:** Automated 3-day gentle bump for prospects who haven't replied.
5. **Tracking:** Track statuses (`pending`, `sent`, `followed_up`, `replied`, `error`).

---

## File Structure

| File | Description |
| :--- | :--- |
| [`outreach.py`](file:///D:/inferreach-outreach/outreach.py) | Main CLI application and email dispatch engine. |
| [`prospects.csv`](file:///D:/inferreach-outreach/prospects.csv) | Staging CSV for prospect details and personalized research angles. |
| [`outreach.db`](file:///D:/inferreach-outreach/outreach.db) | SQLite database tracking outreach status, sent timestamps, and replies. |
| [`.gitignore`](file:///D:/inferreach-outreach/.gitignore) | Git ignore rules ensuring customer/prospect data and SQLite DBs are not committed. |
| [`GEMINI.md`](file:///D:/inferreach-outreach/GEMINI.md) | Agent memory and instructions for Antigravity. |

---

## Configuration & Environment

Set the Resend API key before dispatching emails:

```powershell
$env:RESEND_API_KEY="re_your_api_key_here"
```

Default sender config in [`outreach.py`](file:///D:/inferreach-outreach/outreach.py):
- **From:** `Prajwol <hello@tryinferreach.com>`
- **Reply-To:** `hello@tryinferreach.com`
- **Rate Limit Delay:** 2 seconds between emails

---

## CLI Usage

```powershell
# Preview emails in console without sending (reads directly from prospects.csv)
python outreach.py preview

# Import staged records from prospects.csv into outreach.db (optional: --campaign <name>)
python outreach.py import [--campaign series-a]

# Send initial outreach emails (default limit: 10, optional: --campaign <name>)
python outreach.py send --limit 10 [--campaign series-a]

# Send 3-day follow-up emails
python outreach.py followup --limit 10

# Record a prospect reply with optional sentiment and notes
python outreach.py mark-replied <email> --sentiment interested --notes "Wants 15m demo next Tue"

# Add an email or entire domain to the suppression / DNC list
python outreach.py opt-out <email-or-@domain> --reason opt_out

# View message history & Resend message IDs for a prospect
python outreach.py history <email>

# View the suppression / Do-Not-Contact list
python outreach.py suppression

# Check pipeline metrics and delivery status
python outreach.py status
```

---

## Prospect Data Schema

`prospects.csv` requires the following headers:
- `email`: Prospect email address
- `first_name`: Prospect first name
- `company`: Target company name
- `role`: Prospect job title / function
- `pain_point`: Specific pain point observed
- `recent_news`: Recent news / milestone hook (e.g., funding round, product launch)
