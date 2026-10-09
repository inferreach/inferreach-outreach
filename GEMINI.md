# Workspace Memory: InferReach Outreach Engine

## Project Identity
- **Company:** InferReach ([inferreach.com](https://inferreach.com))
- **Owner / Sender:** Prajwol (`hello@tryinferreach.com`)
- **Domain:** B2B Sales & GTM Cold Outreach
- **Purpose:** This folder contains the code, data, templates, and tracking database for InferReach's outbound email engine.

## Key Files & Structure
- [`outreach.py`](file:///D:/inferreach-outreach/outreach.py): Core outreach engine CLI for previewing, importing, sending, following up, reply tracking, suppression, and prospect history.
- [`prospects.csv`](file:///D:/inferreach-outreach/prospects.csv): Staged prospect list with custom research fields (pain points, recent company news).
- [`outreach.db`](file:///D:/inferreach-outreach/outreach.db): SQLite tracking database with `prospects`, `messages` (outbox & Resend IDs), and `suppression_list` (opt-outs / DNC).
- [`README.md`](file:///D:/inferreach-outreach/README.md): Project documentation and command reference.
- [`.gitignore`](file:///D:/inferreach-outreach/.gitignore): Excludes `.db`, `.csv`, and `.bak` files from version control to prevent leaking prospect data.

## Operational Rules & Safety
1. **Never Send Emails Without Explicit Instruction:** Do not execute `python outreach.py send` or `followup` unless the user explicitly requests sending. Use `preview` or dry-runs during development/testing.
2. **Data Privacy:** Keep `prospects.csv` and `outreach.db` untracked in git. Never expose or commit prospect PII or API keys (`RESEND_API_KEY`).
3. **Tone & Strategy:** The email philosophy is curious, conversational, non-salesy, and research-backed (never generic spam).
