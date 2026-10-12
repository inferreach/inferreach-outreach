#!/usr/bin/env python3
"""
InferReach Outreach Engine — Desktop UI (outreachUI.py)

Run:
    python outreachUI.py
"""

import csv
import json
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
from pathlib import Path
from types import SimpleNamespace

try:
    import resend  # noqa: F401
except ImportError:
    _r = tk.Tk(); _r.withdraw()
    messagebox.showerror("Missing dependency",
                         "The 'resend' package is required.\n\nRun:\n    pip install resend")
    sys.exit(1)

import outreach
from outreach import (
    CONFIG, init_db, build_initial, build_followup,
    get_draft, save_draft, delete_draft, is_suppressed, send_email,
)

SETTINGS_PATH = Path(__file__).with_name("outreach_ui_settings.json")

TEMPLATE_KEYS = ["initial_subject", "initial_body", "followup_subject", "followup_body"]


# ============================================================
# Helpers
# ============================================================

class _QueueWriter:
    def __init__(self, q): self.q = q
    def write(self, text):
        if text: self.q.put(text)
    def flush(self): pass


def _short(ts):
    return "" if not ts else str(ts)[:16].replace("T", " ")


# ============================================================
# Main UI
# ============================================================

class OutreachUI:
    def __init__(self, root):
        self.root = root
        root.title("InferReach Outreach Engine")
        root.geometry("1240x860")
        root.minsize(1040, 700)

        self.queue = queue.Queue()
        self.busy = False
        self._busy_buttons = []
        self._compose_pid = None       # currently loaded prospect id
        self._compose_email = None     # currently loaded prospect email
        self._compose_step = "initial" # "initial" or "followup"

        self._setup_style()
        self._build_layout()
        self._load_settings()
        self.refresh_all()
        self.refresh_sender_info()
        self.refresh_templates()

        self.root.after(80, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------------------------------------
    def _setup_style(self):
        style = ttk.Style()
        try: style.theme_use("clam")
        except tk.TclError: pass

        self.big_font = tkfont.nametofont("TkDefaultFont").copy()
        self.big_font.configure(size=17, weight="bold")
        self.small_font = tkfont.nametofont("TkDefaultFont").copy()
        self.small_font.configure(size=9)
        self.mono_font = tkfont.Font(
            family="Consolas" if sys.platform.startswith("win") else "Courier", size=10)
        self.addr_font = tkfont.Font(
            family="Consolas" if sys.platform.startswith("win") else "Courier",
            size=10, weight="bold")

        style.configure("CardValue.TLabel", font=self.big_font)
        style.configure("CardLabel.TLabel", font=self.small_font, foreground="#555")
        style.configure("Treeview", rowheight=24)

    def _build_layout(self):
        header = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        ttk.Label(header, text="InferReach Outreach Engine",
                  font=("TkDefaultFont", 16, "bold")).pack(side="left")
        ttk.Label(header, text="curious cold emails + soft follow-ups",
                  font=self.small_font, foreground="#777").pack(side="left", padx=(12, 0), pady=(6, 0))
        ttk.Button(header, text="Refresh All", command=self.refresh_all).pack(side="right")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 6))

        self.tab_dashboard   = ttk.Frame(self.nb, padding=10)
        self.tab_prospects   = ttk.Frame(self.nb, padding=10)
        self.tab_compose     = ttk.Frame(self.nb, padding=10)
        self.tab_send        = ttk.Frame(self.nb, padding=10)
        self.tab_preview     = ttk.Frame(self.nb, padding=10)
        self.tab_suppression = ttk.Frame(self.nb, padding=10)
        self.tab_settings    = ttk.Frame(self.nb, padding=10)

        self.nb.add(self.tab_dashboard,   text="  Dashboard  ")
        self.nb.add(self.tab_prospects,   text="  Prospects  ")
        self.nb.add(self.tab_compose,     text="  Compose  ")
        self.nb.add(self.tab_send,        text="  Send  ")
        self.nb.add(self.tab_preview,     text="  Preview  ")
        self.nb.add(self.tab_suppression, text="  Suppression  ")
        self.nb.add(self.tab_settings,    text="  Settings  ")

        self._build_dashboard()
        self._build_prospects()
        self._build_compose()
        self._build_send()
        self._build_preview()
        self._build_suppression()
        self._build_settings()

        console_frame = ttk.LabelFrame(self.root, text="Console Output", padding=6)
        console_frame.pack(fill="both", padx=12, pady=(0, 4))
        self.console = scrolledtext.ScrolledText(
            console_frame, height=9, wrap="word", state="disabled",
            font=("Consolas", 9) if sys.platform.startswith("win") else ("Courier", 9),
            background="#1e1e1e", foreground="#d4d4d4", insertbackground="#d4d4d4")
        self.console.pack(fill="both", expand=True)
        self.console.tag_config("err", foreground="#ff6b6b")

        btn_row = ttk.Frame(console_frame); btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(btn_row, text="Clear Console", command=self._clear_console).pack(side="right")

        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Frame(self.root, padding=(12, 0, 12, 8)); status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var, font=self.small_font,
                  foreground="#555").pack(side="left")

    # ========================================================
    # DASHBOARD
    # ========================================================
    def _build_dashboard(self):
        cards = ttk.Frame(self.tab_dashboard); cards.pack(fill="x", pady=(0, 10))
        self.card_vars = {}
        defs = [
            ("total", "TOTAL"), ("pending", "PENDING"), ("sent", "SENT"),
            ("followed_up", "FOLLOWED UP"), ("replied", "REPLIED"),
            ("reply_rate", "REPLY RATE"), ("drafts", "DRAFTS"),
            ("opted_out", "OPTED OUT"), ("suppressed", "SUPPRESSED"),
        ]
        for i, (key, label) in enumerate(defs):
            card = ttk.Frame(cards, relief="solid", borderwidth=1, padding=(12, 8))
            card.grid(row=0, column=i, sticky="nsew", padx=3)
            cards.columnconfigure(i, weight=1)
            var = tk.StringVar(value="—"); self.card_vars[key] = var
            ttk.Label(card, textvariable=var, style="CardValue.TLabel").pack(anchor="w")
            ttk.Label(card, text=label, style="CardLabel.TLabel").pack(anchor="w")

        grid = ttk.Frame(self.tab_dashboard); grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1); grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1); grid.rowconfigure(1, weight=1)
        self.status_tree = self._make_tree(grid, "Prospect Status",
            ("status", "count"), ("Status", "Count"), (0, 0), (160, 80))
        self.campaign_tree = self._make_tree(grid, "Campaigns",
            ("campaign", "count"), ("Campaign", "Count"), (0, 1), (200, 80))
        self.sentiment_tree = self._make_tree(grid, "Reply Sentiments",
            ("sentiment", "count"), ("Sentiment", "Count"), (1, 0), (160, 80))
        self.message_tree = self._make_tree(grid, "Dispatched Messages",
            ("step", "status", "count"), ("Step Type", "Status", "Count"), (1, 1), (140, 100, 70))

    def _make_tree(self, parent, title, cols, headings, gridpos, widths):
        frame = ttk.LabelFrame(parent, text=title, padding=6)
        frame.grid(row=gridpos[0], column=gridpos[1], sticky="nsew", padx=4, pady=4)
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=5)
        for col, head, w in zip(cols, headings, widths):
            tree.heading(col, text=head); tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        return tree

    def refresh_dashboard(self):
        conn = init_db(); c = conn.cursor()
        try:
            c.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status")
            sc = dict(c.fetchall())
            c.execute("SELECT campaign, COUNT(*) FROM prospects GROUP BY campaign")
            campaigns = c.fetchall()
            c.execute("""SELECT sentiment, COUNT(*) FROM prospects
                         WHERE status='replied' AND sentiment IS NOT NULL GROUP BY sentiment""")
            sentiments = c.fetchall()
            c.execute("SELECT step_type, status, COUNT(*) FROM messages GROUP BY step_type, status")
            messages = c.fetchall()
            c.execute("SELECT COUNT(*) FROM suppression_list")
            suppressed = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM drafts")
            drafts = c.fetchone()[0]
        finally:
            conn.close()

        total = sum(sc.values())
        replied = sc.get("replied", 0)
        self.card_vars["total"].set(str(total))
        self.card_vars["pending"].set(str(sc.get("pending", 0)))
        self.card_vars["sent"].set(str(sc.get("sent", 0)))
        self.card_vars["followed_up"].set(str(sc.get("followed_up", 0)))
        self.card_vars["replied"].set(str(replied))
        self.card_vars["reply_rate"].set(f"{(replied/total*100):.1f}%" if total else "0.0%")
        self.card_vars["drafts"].set(str(drafts))
        self.card_vars["opted_out"].set(str(sc.get("opted_out", 0)))
        self.card_vars["suppressed"].set(str(suppressed))

        self._fill(self.status_tree, sorted(sc.items()))
        self._fill(self.campaign_tree, [(c or "default", n) for c, n in campaigns])
        self._fill(self.sentiment_tree, sentiments)
        self._fill(self.message_tree, messages)

    @staticmethod
    def _fill(tree, rows):
        tree.delete(*tree.get_children())
        for r in rows: tree.insert("", "end", values=r)

    # ========================================================
    # PROSPECTS TAB
    # ========================================================
    def _build_prospects(self):
        tb = ttk.Frame(self.tab_prospects); tb.pack(fill="x", pady=(0, 8))
        ttk.Label(tb, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        e = ttk.Entry(tb, textvariable=self.search_var, width=28)
        e.pack(side="left", padx=(4, 8)); e.bind("<Return>", lambda _: self.refresh_prospects())
        ttk.Button(tb, text="Refresh", command=self.refresh_prospects).pack(side="left")
        ttk.Button(tb, text="Clear", command=self._clear_search).pack(side="left", padx=4)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8)
        b_imp = ttk.Button(tb, text="Import CSV", command=self.do_import); b_imp.pack(side="left")
        b_prev = ttk.Button(tb, text="Preview CSV", command=self.do_preview_from_dialog); b_prev.pack(side="left", padx=4)
        ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=8)
        b_comp = ttk.Button(tb, text="Compose Email", command=self.do_compose_selected); b_comp.pack(side="left")
        b_hist = ttk.Button(tb, text="History", command=self.do_history_selected); b_hist.pack(side="left", padx=4)
        b_rep = ttk.Button(tb, text="Mark Replied", command=self.do_mark_replied_selected); b_rep.pack(side="left")
        b_opt = ttk.Button(tb, text="Opt Out", command=self.do_optout_selected); b_opt.pack(side="left", padx=4)
        self._busy_buttons.extend([b_imp, b_prev, b_comp, b_hist, b_rep, b_opt])

        frame = ttk.Frame(self.tab_prospects); frame.pack(fill="both", expand=True)
        cols = ("id", "email", "name", "company", "role", "campaign",
                "status", "draft", "sent", "followed", "replied", "sentiment")
        heads = ("ID", "Email (recipient)", "First Name", "Company", "Role", "Campaign",
                 "Status", "Draft?", "Sent At", "Followed Up", "Replied At", "Sentiment")
        widths = (45, 220, 100, 140, 120, 90, 95, 65, 120, 120, 120, 100)
        self.prospect_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col, head, w in zip(cols, heads, widths):
            self.prospect_tree.heading(col, text=head)
            self.prospect_tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.prospect_tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.prospect_tree.xview)
        self.prospect_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.prospect_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns"); hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)

        self.prospect_tree.tag_configure("replied", background="#e6f7e6")
        self.prospect_tree.tag_configure("opted_out", background="#fde8e8")
        self.prospect_tree.tag_configure("failed", background="#fff4e5")
        self.prospect_tree.tag_configure("followed_up", background="#eef4fd")
        self.prospect_tree.tag_configure("draft", background="#fffbe6")

        self.prospect_tree.bind("<Double-1>", lambda _: self.do_compose_selected())

        self.prospect_count_var = tk.StringVar(value="0 prospects")
        ttk.Label(self.tab_prospects, textvariable=self.prospect_count_var,
                  font=self.small_font, foreground="#666").pack(anchor="w", pady=(4, 0))

    def _clear_search(self):
        self.search_var.set(""); self.refresh_prospects()

    def refresh_prospects(self):
        self.prospect_tree.delete(*self.prospect_tree.get_children())
        conn = init_db(); c = conn.cursor()
        try:
            q = self.search_var.get().strip().lower()
            base = """
                SELECT p.id, p.email, p.first_name, p.company, p.role, p.campaign, p.status,
                       (SELECT COUNT(*) FROM drafts d WHERE d.prospect_id = p.id) AS draft_ct,
                       p.sent_at, p.followed_up_at, p.replied_at, p.sentiment
                FROM prospects p
            """
            if q:
                like = f"%{q}%"
                c.execute(base + """
                    WHERE lower(p.email) LIKE ? OR lower(p.first_name) LIKE ?
                       OR lower(p.company) LIKE ? OR lower(p.role) LIKE ?
                    ORDER BY p.id DESC
                """, (like, like, like, like))
            else:
                c.execute(base + " ORDER BY p.id DESC")
            rows = c.fetchall()
        finally:
            conn.close()

        for r in rows:
            (pid, email, fn, comp, role, camp, status, draft_ct,
             sent, fu, rep, senti) = r
            tag = "draft" if draft_ct else (
                status if status in ("replied", "opted_out", "failed", "followed_up") else "")
            draft_flag = "✏ yes" if draft_ct else ""
            self.prospect_tree.insert("", "end", tags=(tag,), values=(
                pid, email or "", fn or "", comp or "", role or "", camp or "default",
                status or "", draft_flag, _short(sent), _short(fu), _short(rep), senti or ""))
        self.prospect_count_var.set(f"{len(rows)} prospect(s) shown")

    def _selected_prospect(self):
        sel = self.prospect_tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a prospect row first.")
            return None
        vals = self.prospect_tree.item(sel[0], "values")
        return int(vals[0]), vals[1]

    def do_compose_selected(self):
        sel = self._selected_prospect()
        if not sel: return
        pid, email = sel
        self.load_into_compose(pid, email)
        self.nb.select(self.tab_compose)

    def do_history_selected(self):
        sel = self._selected_prospect()
        if not sel: return
        _, email = sel
        self._run(lambda: outreach.cmd_history(SimpleNamespace(email=email)))

    def do_optout_selected(self):
        sel = self._selected_prospect()
        if not sel: return
        _, email = sel
        reason = simpledialog.askstring("Opt Out",
            f"Add '{email}' to the suppression list.\n\nReason:",
            initialvalue="opt_out", parent=self.root)
        if reason is None: return
        self._run(lambda: outreach.cmd_opt_out(SimpleNamespace(target=email, reason=reason or "opt_out")),
                  on_done=self.refresh_all)

    def do_mark_replied_selected(self):
        sel = self._selected_prospect()
        if not sel: return
        _, email = sel
        self._mark_replied_dialog(email)

    def _mark_replied_dialog(self, email):
        dlg = tk.Toplevel(self.root); dlg.title("Mark Replied")
        dlg.transient(self.root); dlg.grab_set(); dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16); frm.pack(fill="both", expand=True)
        ttk.Label(frm, text=f"Prospect: {email}",
                  font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                                          sticky="w", pady=(0, 12))
        ttk.Label(frm, text="Sentiment:").grid(row=1, column=0, sticky="w", pady=4)
        sv = tk.StringVar(value="interested")
        ttk.Combobox(frm, textvariable=sv, state="readonly", width=22,
                     values=["interested", "not_interested", "not_now", "wrong_person", "booked", ""]
                     ).grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(frm, text="Notes:").grid(row=2, column=0, sticky="nw", pady=4)
        notes = tk.Text(frm, width=40, height=5, wrap="word"); notes.grid(row=2, column=1, sticky="w", pady=4)
        btns = ttk.Frame(frm); btns.grid(row=3, column=0, columnspan=2, sticky="e", pady=(12, 0))
        def submit():
            senti = sv.get().strip() or None
            note = notes.get("1.0", "end").strip()
            dlg.destroy()
            self._run(lambda: outreach.cmd_mark_replied(
                SimpleNamespace(email=email, sentiment=senti, notes=note)),
                on_done=self.refresh_all)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="Save", command=submit).pack(side="right")

    def do_import(self):
        path = filedialog.askopenfilename(
            title="Select prospects CSV", initialdir=str(Path.cwd()),
            initialfile=CONFIG["csv_path"],
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path: return
        campaign = simpledialog.askstring("Campaign",
            "Campaign / batch tag for these prospects:",
            initialvalue="default", parent=self.root)
        if campaign is None: return
        self._run(lambda: outreach.cmd_import(
            SimpleNamespace(file=path, campaign=campaign.strip() or "default")),
            on_done=self.refresh_all)

    # ========================================================
    # COMPOSE TAB
    # ========================================================
    def _build_compose(self):
        top = ttk.LabelFrame(self.tab_compose, text="Load a Prospect", padding=10)
        top.pack(fill="x", pady=(0, 8))

        ttk.Label(top, text="Prospect:").grid(row=0, column=0, sticky="w")
        self.compose_email_var = tk.StringVar(value="")
        self.compose_combo = ttk.Combobox(
            top, textvariable=self.compose_email_var, width=60, state="readonly")
        self.compose_combo.grid(row=0, column=1, padx=8, sticky="w")
        ttk.Button(top, text="↻ Refresh List", command=self._refresh_compose_list).grid(row=0, column=2, padx=4)

        ttk.Label(top, text="Step:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.compose_step_var = tk.StringVar(value="initial")
        step_cb = ttk.Combobox(top, textvariable=self.compose_step_var, width=12,
                               state="readonly", values=["initial", "followup"])
        step_cb.grid(row=1, column=1, padx=8, sticky="w", pady=(8, 0))
        step_cb.bind("<<ComboboxSelected>>", lambda _: self._on_compose_step_change())

        self.compose_from_var = tk.StringVar(value="FROM:     —")
        self.compose_reply_var = tk.StringVar(value="REPLY-TO: —")
        self.compose_to_var = tk.StringVar(value="TO:       —")
        for i, v in enumerate([self.compose_from_var, self.compose_reply_var, self.compose_to_var]):
            ttk.Label(top, textvariable=v, font=self.addr_font,
                      foreground="#00695c" if i == 0 else "#333").grid(
                row=2 + i, column=0, columnspan=3, sticky="w", pady=(6 if i == 0 else 2, 0))

        body_frame = ttk.LabelFrame(self.tab_compose, text="Message Editor", padding=10)
        body_frame.pack(fill="both", expand=True)

        ttk.Label(body_frame, text="Subject:").grid(row=0, column=0, sticky="w")
        self.compose_subject_var = tk.StringVar()
        ttk.Entry(body_frame, textvariable=self.compose_subject_var, width=90).grid(
            row=0, column=1, sticky="ew", padx=8)

        ttk.Label(body_frame, text="Body:").grid(row=1, column=0, sticky="nw", pady=(8, 0))
        self.compose_body = scrolledtext.ScrolledText(
            body_frame, wrap="word", height=18,
            font=("Consolas", 10) if sys.platform.startswith("win") else ("Courier", 10))
        self.compose_body.grid(row=1, column=1, sticky="nsew", padx=8, pady=(8, 0))
        body_frame.columnconfigure(1, weight=1)
        body_frame.rowconfigure(1, weight=1)

        actions = ttk.Frame(self.tab_compose); actions.pack(fill="x", pady=(10, 0))
        b_reload = ttk.Button(actions, text="↻ Load Fresh Template",
                              command=self._reload_template_into_editor); b_reload.pack(side="left")
        b_save = ttk.Button(actions, text="💾 Save Draft", command=self.do_save_draft); b_save.pack(side="left", padx=6)
        b_send = ttk.Button(actions, text="📤 Send Now", command=self.do_send_now); b_send.pack(side="left", padx=6)
        b_del = ttk.Button(actions, text="🗑 Delete Draft", command=self.do_delete_draft); b_del.pack(side="left", padx=6)

        self.compose_status_var = tk.StringVar(value="No prospect loaded.")
        ttk.Label(actions, textvariable=self.compose_status_var,
                  font=self.small_font, foreground="#555").pack(side="right")

        self._busy_buttons.extend([b_reload, b_save, b_send, b_del])

        self._refresh_compose_list()

    def _refresh_compose_list(self):
        conn = init_db(); c = conn.cursor()
        try:
            c.execute("""SELECT id, email, first_name, company, campaign, status
                         FROM prospects ORDER BY id DESC""")
            self._compose_prospects = c.fetchall()
        finally:
            conn.close()
        labels = [f"#{pid}  {email}  —  {comp or ''}  [{status}]"
                  for pid, email, fn, comp, camp, status in self._compose_prospects]
        self.compose_combo["values"] = labels
        if labels and self.compose_email_var.get() not in labels:
            self.compose_email_var.set(labels[0])
            self._on_compose_select()

    def _on_compose_select(self):
        idx = self.compose_combo.current()
        if idx < 0 or idx >= len(self._compose_prospects):
            return
        pid, email, *_ = self._compose_prospects[idx]
        self.load_into_compose(pid, email)

    def _on_compose_step_change(self):
        if self._compose_pid is None: return
        self.load_into_compose(self._compose_pid, self._compose_email)

    def load_into_compose(self, pid, email):
        """Load a prospect into the compose editor."""
        self._compose_pid = pid
        self._compose_email = email

        # Sync combo selection
        labels = list(self.compose_combo["values"])
        for i, lbl in enumerate(labels):
            if lbl.startswith(f"#{pid} "):
                self.compose_combo.current(i)
                break

        step = self.compose_step_var.get() or "initial"
        self._compose_step = step

        conn = init_db(); c = conn.cursor()
        try:
            c.execute("""SELECT first_name, company, role, pain_point, recent_news
                         FROM prospects WHERE id = ?""", (pid,))
            row = c.fetchone()
            draft = get_draft(conn, pid, step)
        finally:
            conn.close()

        if not row:
            self.compose_status_var.set("Prospect not found.")
            return
        fn, comp, role, pain, news = row
        prospect = {"first_name": fn, "company": comp, "role": role,
                    "pain_point": pain, "recent_news": news}

        if draft:
            subject, body = draft
            self.compose_status_var.set(f"Draft loaded for {email} ({step}).")
        else:
            rendered = build_initial(prospect) if step == "initial" else build_followup(prospect)
            subject, body = rendered["subject"], rendered["body"]
            self.compose_status_var.set(f"Fresh template loaded for {email} ({step}). Not saved yet.")

        self.compose_subject_var.set(subject)
        self.compose_body.delete("1.0", "end")
        self.compose_body.insert("1.0", body)

        self.compose_from_var.set(
            f"FROM:     {CONFIG['sender_name']} <{CONFIG['sender_email']}>")
        self.compose_reply_var.set(f"REPLY-TO: {CONFIG['reply_to']}")
        self.compose_to_var.set(f"TO:       {email}")

    def _reload_template_into_editor(self):
        if self._compose_pid is None:
            messagebox.showinfo("No prospect", "Load a prospect first.")
            return
        if not messagebox.askyesno("Reload template",
            "Discard the current editor content and reload the base template?\n\n"
            "Any unsaved edits will be lost."):
            return
        # Delete any saved draft, then reload fresh template
        conn = init_db()
        delete_draft(conn, self._compose_pid, self._compose_step)
        conn.close()
        self.load_into_compose(self._compose_pid, self._compose_email)
        self.refresh_prospects()

    def _editor_content(self):
        subject = self.compose_subject_var.get().strip()
        body = self.compose_body.get("1.0", "end").rstrip()
        return subject, body

    def do_save_draft(self):
        if self._compose_pid is None:
            messagebox.showinfo("No prospect", "Load a prospect first.")
            return
        subject, body = self._editor_content()
        if not subject:
            messagebox.showwarning("Missing subject", "Please enter a subject.")
            return
        conn = init_db()
        save_draft(conn, self._compose_pid, subject, body, self._compose_step)
        conn.close()
        self.compose_status_var.set(
            f"✓ Draft saved for {self._compose_email} ({self._compose_step}).")
        self.log(f"[draft] Saved {self._compose_step} draft for {self._compose_email}\n")
        self.refresh_prospects()
        self.refresh_dashboard()

    def do_delete_draft(self):
        if self._compose_pid is None:
            messagebox.showinfo("No prospect", "Load a prospect first.")
            return
        conn = init_db()
        delete_draft(conn, self._compose_pid, self._compose_step)
        conn.close()
        self.compose_status_var.set(
            f"Draft deleted for {self._compose_email} ({self._compose_step}).")
        self.log(f"[draft] Deleted {self._compose_step} draft for {self._compose_email}\n")
        self.refresh_prospects()
        self.refresh_dashboard()

    def do_send_now(self):
        if self._compose_pid is None:
            messagebox.showinfo("No prospect", "Load a prospect first.")
            return
        if not CONFIG.get("resend_api_key"):
            messagebox.showerror("Missing API key",
                "Set your Resend API key in Settings first.")
            return

        subject, body = self._editor_content()
        if not subject:
            messagebox.showwarning("Missing subject", "Please enter a subject.")
            return

        email = self._compose_email
        step = self._compose_step
        sender = f"{CONFIG['sender_name']} <{CONFIG['sender_email']}>"
        if not messagebox.askyesno("Confirm send",
            f"Send this email now?\n\n"
            f"FROM:     {sender}\n"
            f"REPLY-TO: {CONFIG['reply_to']}\n"
            f"TO:       {email}\n"
            f"SUBJECT:  {subject}\n\n"
            f"This will send a real email via Resend."):
            return

        pid = self._compose_pid

        def task():
            import outreach as out
            out.resend.api_key = CONFIG["resend_api_key"]
            conn = init_db()
            try:
                if is_suppressed(conn, email)[0]:
                    print(f"[skip] {email} is on the suppression list.")
                    return
                print(f"Sending 1 composed email...\n")
                print(f"  FROM:     {sender}")
                print(f"  REPLY-TO: {CONFIG['reply_to']}\n")
                msg_id = send_email(email, subject, body)
                from datetime import datetime
                now_iso = datetime.now().isoformat()
                c = conn.cursor()
                if msg_id:
                    step_number = 1 if step == "initial" else 2
                    c.execute("""
                        INSERT INTO messages (prospect_id, step_number, step_type,
                            resend_id, subject, body, status, sent_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'sent', ?)
                    """, (pid, step_number, step, msg_id, subject, body, now_iso))
                    if step == "initial":
                        c.execute("UPDATE prospects SET status='sent', sent_at=?, updated_at=? WHERE id=?",
                                  (now_iso, now_iso, pid))
                    else:
                        c.execute("UPDATE prospects SET status='followed_up', followed_up_at=?, updated_at=? WHERE id=?",
                                  (now_iso, now_iso, pid))
                    delete_draft(conn, pid, step)
                    conn.commit()
                    print(f"  Sent FROM <{CONFIG['sender_email']}> -> TO <{email}> (id: {msg_id})")
                else:
                    c.execute("""
                        INSERT INTO messages (prospect_id, step_number, step_type,
                            subject, body, status, sent_at, error)
                        VALUES (?, ?, ?, ?, ?, 'failed', ?, ?)
                    """, (pid, step_number, step, subject, body, now_iso, f"{step}_failed"))
                    c.execute("UPDATE prospects SET error=?, updated_at=? WHERE id=?",
                              (f"{step}_failed", now_iso, pid))
                    conn.commit()
                    print(f"  FAILED -> TO <{email}>")
            finally:
                conn.close()

        self._run(task, on_done=self._after_compose_send)

    def _after_compose_send(self):
        self.refresh_all()
        # Reload the fresh template
        if self._compose_pid is not None:
            self.load_into_compose(self._compose_pid, self._compose_email)

    # ========================================================
    # SEND TAB
    # ========================================================
    def _build_send(self):
        sender_frame = ttk.LabelFrame(self.tab_send, text="Currently Sending As", padding=12)
        sender_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(sender_frame, text="FROM:", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=2)
        self.from_addr_var = tk.StringVar(value="—")
        ttk.Label(sender_frame, textvariable=self.from_addr_var,
                  font=self.addr_font, foreground="#00695c").grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(sender_frame, text="REPLY-TO:", font=("TkDefaultFont", 10, "bold")).grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=2)
        self.replyto_addr_var = tk.StringVar(value="—")
        ttk.Label(sender_frame, textvariable=self.replyto_addr_var,
                  font=self.addr_font, foreground="#1565c0").grid(row=1, column=1, sticky="w", pady=2)

        ttk.Label(sender_frame,
            text="Every outgoing email uses the FROM address. Replies route to the REPLY-TO "
                 "address. Any saved drafts will be used instead of the template.",
            font=self.small_font, foreground="#777", wraplength=980, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        top = ttk.LabelFrame(self.tab_send, text="Dispatch Controls", padding=14)
        top.pack(fill="x")
        ttk.Label(top, text="Max emails to send:").grid(row=0, column=0, sticky="w", pady=6)
        self.limit_var = tk.IntVar(value=10)
        ttk.Spinbox(top, from_=1, to=500, textvariable=self.limit_var, width=8).grid(
            row=0, column=1, sticky="w", padx=8)
        ttk.Label(top, text="Campaign filter (optional):").grid(row=1, column=0, sticky="w", pady=6)
        self.campaign_filter_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.campaign_filter_var, width=24).grid(
            row=1, column=1, sticky="w", padx=8)
        ttk.Label(top, text="Leave the campaign blank to include every campaign.",
                  font=self.small_font, foreground="#777").grid(row=1, column=2, sticky="w", padx=8)

        actions = ttk.Frame(self.tab_send); actions.pack(fill="x", pady=14)
        b_new = ttk.Button(actions, text="Send to NEW Only (Safe)", width=26,
                           command=self.do_send_new_only); b_new.pack(side="left", padx=(0, 8))
        b_init = ttk.Button(actions, text="Send Initial Emails", width=24,
                            command=self.do_send_initial); b_init.pack(side="left", padx=(0, 8))
        b_fu = ttk.Button(actions, text="Send 3-Day Follow-Ups", width=24,
                          command=self.do_send_followups); b_fu.pack(side="left")
        self._busy_buttons.extend([b_new, b_init, b_fu])

        info = ttk.LabelFrame(self.tab_send, text="How this works", padding=12)
        info.pack(fill="both", expand=True)
        text = (
            "• Send to NEW Only (Safe)  →  only emails prospects who have NEVER received any\n"
            "  message (no initial, no follow-up, no failures logged).\n\n"
            "• Send Initial Emails  →  sends to every prospect with status 'pending'.\n\n"
            "• Send 3-Day Follow-Ups  →  sends follow-ups to 'sent' prospects 3+ days old.\n\n"
            "• Saved drafts take priority. If you've composed a custom email in the Compose tab\n"
            "  and saved it as a draft, the send commands will use that instead of the template,\n"
            "  then consume it after a successful send.\n\n"
            "• Edit the base templates in the Settings tab."
        )
        ttk.Label(info, text=text, justify="left", font=("TkDefaultFont", 10)).pack(anchor="w")

    def refresh_sender_info(self):
        sn = CONFIG.get("sender_name", "").strip()
        se = CONFIG.get("sender_email", "").strip()
        rt = CONFIG.get("reply_to", "").strip()
        self.from_addr_var.set(f"{sn} <{se}>" if sn else (se or "(not set)"))
        self.replyto_addr_var.set(rt or "(not set)")

    def _api_key_ok(self):
        if not CONFIG.get("resend_api_key"):
            messagebox.showerror("Missing API key",
                "RESEND_API_KEY is not set.\n\nGo to the Settings tab and paste your Resend API key.")
            return False
        return True

    def _confirm_send(self, title, limit, scope, extra_note=""):
        sender = f"{CONFIG.get('sender_name', '')} <{CONFIG.get('sender_email', '')}>"
        msg = (f"Send up to {limit} email(s) to prospects in {scope}?\n\n"
               f"FROM:     {sender}\nREPLY-TO: {CONFIG.get('reply_to', '')}\n")
        if extra_note: msg += f"\n{extra_note}"
        return messagebox.askyesno(title, msg)

    def do_send_new_only(self):
        if not self._api_key_ok(): return
        limit = self.limit_var.get()
        campaign = self.campaign_filter_var.get().strip() or None
        scope = f"campaign '{campaign}'" if campaign else "ALL campaigns"
        if not self._confirm_send("Confirm safe send", limit, scope,
            "Only emails prospects who have NEVER received any message."):
            return
        self._run(lambda: outreach.cmd_send_new(SimpleNamespace(limit=limit, campaign=campaign)),
                  on_done=self.refresh_all)

    def do_send_initial(self):
        if not self._api_key_ok(): return
        limit = self.limit_var.get()
        campaign = self.campaign_filter_var.get().strip() or None
        scope = f"campaign '{campaign}'" if campaign else "ALL campaigns"
        if not self._confirm_send("Confirm send", limit, scope,
            "This will send real emails via Resend."):
            return
        self._run(lambda: outreach.cmd_send(SimpleNamespace(limit=limit, campaign=campaign)),
                  on_done=self.refresh_all)

    def do_send_followups(self):
        if not self._api_key_ok(): return
        limit = self.limit_var.get()
        campaign = self.campaign_filter_var.get().strip() or None
        scope = f"campaign '{campaign}'" if campaign else "ALL campaigns"
        if not self._confirm_send("Confirm follow-ups", limit, scope,
            "Only prospects whose initial email is 3+ days old and who haven't replied."):
            return
        self._run(lambda: outreach.cmd_followup(SimpleNamespace(limit=limit, campaign=campaign)),
                  on_done=self.refresh_all)

    # ========================================================
    # PREVIEW TAB
    # ========================================================
    def _build_preview(self):
        bar = ttk.Frame(self.tab_preview); bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="CSV file:").pack(side="left")
        self.preview_file_var = tk.StringVar(value=CONFIG["csv_path"])
        ttk.Entry(bar, textvariable=self.preview_file_var, width=52).pack(side="left", padx=6)
        ttk.Button(bar, text="Browse…", command=self._browse_preview_file).pack(side="left")
        ttk.Button(bar, text="Preview", command=self.do_preview).pack(side="left", padx=6)

        self.preview_text = scrolledtext.ScrolledText(
            self.tab_preview, wrap="word", state="disabled",
            font=("Consolas", 10) if sys.platform.startswith("win") else ("Courier", 10))
        self.preview_text.pack(fill="both", expand=True)

    def _browse_preview_file(self):
        path = filedialog.askopenfilename(
            title="Select prospects CSV", initialdir=str(Path.cwd()),
            initialfile=CONFIG["csv_path"],
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path: self.preview_file_var.set(path)

    def do_preview_from_dialog(self):
        self._browse_preview_file(); self.do_preview(); self.nb.select(self.tab_preview)

    def do_preview(self):
        path = self.preview_file_var.get().strip() or CONFIG["csv_path"]
        if not Path(path).exists():
            messagebox.showerror("File not found", f"Could not find:\n{path}"); return
        try:
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except Exception as e:
            messagebox.showerror("Read error", f"Could not read the CSV:\n{e}"); return

        self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", "end")
        if not rows:
            self.preview_text.insert("end", "The CSV is empty.\n")
        else:
            for row in rows:
                data = build_initial(row)
                self.preview_text.insert("end", "=" * 76 + "\n")
                self.preview_text.insert("end",
                    f"FROM:     {CONFIG.get('sender_name','')} <{CONFIG.get('sender_email','')}>\n")
                self.preview_text.insert("end", f"REPLY-TO: {CONFIG.get('reply_to','')}\n")
                self.preview_text.insert("end", f"TO:       {row.get('email','')}\n")
                self.preview_text.insert("end", f"SUBJECT:  {data['subject']}\n")
                self.preview_text.insert("end", "-" * 76 + "\n")
                self.preview_text.insert("end", data["body"] + "\n\n")
            self.preview_text.insert("end",
                f"{len(rows)} email(s) previewed. Nothing was sent.\n")
        self.preview_text.config(state="disabled")
        self.set_status(f"Previewed {len(rows)} email(s) from {path}")

    # ========================================================
    # SUPPRESSION TAB
    # ========================================================
    def _build_suppression(self):
        bar = ttk.LabelFrame(self.tab_suppression, text="Add to Suppression List", padding=10)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text="Email or @domain:").grid(row=0, column=0, sticky="w")
        self.supp_target_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.supp_target_var, width=34).grid(row=0, column=1, padx=8, sticky="w")
        ttk.Label(bar, text="Reason:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.supp_reason_var = tk.StringVar(value="opt_out")
        ttk.Combobox(bar, textvariable=self.supp_reason_var, width=16, state="readonly",
                     values=["opt_out", "bounced", "manual", "complaint"]).grid(row=0, column=3, padx=8, sticky="w")
        b_add = ttk.Button(bar, text="Add", command=self.do_add_suppression); b_add.grid(row=0, column=4, padx=8)
        self._busy_buttons.append(b_add)
        ttk.Label(bar, text="Examples:  tom@acme.com   •   @acme.com (whole domain)",
                  font=self.small_font, foreground="#777").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

        frame = ttk.Frame(self.tab_suppression); frame.pack(fill="both", expand=True)
        cols = ("id", "target", "reason", "created")
        self.supp_tree = ttk.Treeview(frame, columns=cols, show="headings")
        for col, head, w in zip(cols, ("ID", "Target", "Reason", "Added"), (60, 320, 160, 200)):
            self.supp_tree.heading(col, text=head); self.supp_tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.supp_tree.yview)
        self.supp_tree.configure(yscrollcommand=vsb.set)
        self.supp_tree.pack(side="left", fill="both", expand=True); vsb.pack(side="right", fill="y")

        bottom = ttk.Frame(self.tab_suppression); bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(bottom, text="Refresh", command=self.refresh_suppression).pack(side="left")
        self.supp_count_var = tk.StringVar(value="0 suppressed")
        ttk.Label(bottom, textvariable=self.supp_count_var,
                  font=self.small_font, foreground="#666").pack(side="left", padx=10)

    def do_add_suppression(self):
        target = self.supp_target_var.get().strip()
        if not target:
            messagebox.showinfo("Nothing to add", "Enter an email or @domain first."); return
        reason = self.supp_reason_var.get().strip() or "opt_out"
        self.supp_target_var.set("")
        self._run(lambda: outreach.cmd_opt_out(SimpleNamespace(target=target, reason=reason)),
                  on_done=self.refresh_all)

    def refresh_suppression(self):
        self.supp_tree.delete(*self.supp_tree.get_children())
        conn = init_db(); c = conn.cursor()
        try:
            c.execute("SELECT id, email, domain, reason, created_at FROM suppression_list ORDER BY id DESC")
            rows = c.fetchall()
        finally:
            conn.close()
        for sid, email, domain, reason, created in rows:
            target = email if email else f"@{domain}"
            self.supp_tree.insert("", "end", values=(sid, target, reason, _short(created)))
        self.supp_count_var.set(f"{len(rows)} suppressed target(s)")

    # ========================================================
    # SETTINGS TAB
    # ========================================================
    def _build_settings(self):
        nb = ttk.Notebook(self.tab_settings)
        nb.pack(fill="both", expand=True)

        # --- General settings sub-tab ---
        general = ttk.Frame(nb, padding=12)
        nb.add(general, text="  General  ")

        frm = ttk.LabelFrame(general, text="Configuration", padding=16)
        frm.pack(fill="x")

        self.setting_vars = {}
        def add_row(r, label, key, show=None, width=52):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=6)
            var = tk.StringVar(); self.setting_vars[key] = var
            ttk.Entry(frm, textvariable=var, width=width, show=show).grid(
                row=r, column=1, sticky="w", padx=10, pady=6)

        add_row(0, "Resend API Key", "resend_api_key", show="•")
        add_row(1, "Sender Email (FROM)", "sender_email")
        add_row(2, "Sender Name", "sender_name")
        add_row(3, "Reply-To Email", "reply_to")
        add_row(4, "Database Path", "db_path")
        add_row(5, "Default CSV Path", "csv_path")
        add_row(6, "Delay Between Sends (seconds)", "delay_seconds", width=10)

        ttk.Label(frm, text="Signature").grid(row=7, column=0, sticky="nw", pady=6)
        self.signature_text = tk.Text(frm, width=52, height=5, wrap="word")
        self.signature_text.grid(row=7, column=1, sticky="w", padx=10, pady=6)

        btns = ttk.Frame(general); btns.pack(fill="x", pady=12)
        ttk.Button(btns, text="Save All Settings", command=self._save_settings).pack(side="left")
        ttk.Button(btns, text="Reload", command=self._load_settings).pack(side="left", padx=8)

        ttk.Label(general,
            text="Settings are saved to outreach_ui_settings.json next to this script.",
            font=self.small_font, foreground="#666").pack(anchor="w")

        # --- Templates sub-tab ---
        templates = ttk.Frame(nb, padding=12)
        nb.add(templates, text="  Templates  ")

        ttk.Label(templates,
            text="Edit the base templates used to render emails. Placeholders:  "
                 "{first_name}  {company}  {role}  {saw_your_company_line}  {pain_line}  {signature}",
            font=self.small_font, foreground="#555", wraplength=1000, justify="left").pack(anchor="w", pady=(0, 8))

        self.template_widgets = {}

        def add_template(key, label, height=None):
            frame = ttk.LabelFrame(templates, text=label, padding=8)
            frame.pack(fill="x", pady=4)
            if height:
                widget = scrolledtext.ScrolledText(frame, wrap="word", height=height,
                    font=("Consolas", 10) if sys.platform.startswith("win") else ("Courier", 10))
                widget.pack(fill="both", expand=True)
            else:
                var = tk.StringVar()
                widget = ttk.Entry(frame, textvariable=var)
                widget.var = var
                widget.pack(fill="x")
            self.template_widgets[key] = widget

        add_template("initial_subject", "Initial Email — Subject")
        add_template("initial_body", "Initial Email — Body", height=10)
        add_template("followup_subject", "Follow-up Email — Subject")
        add_template("followup_body", "Follow-up Email — Body", height=8)

        tbtns = ttk.Frame(templates); tbtns.pack(fill="x", pady=10)
        ttk.Button(tbtns, text="Save Templates", command=self._save_templates).pack(side="left")
        ttk.Button(tbtns, text="Reset to Defaults",
                   command=self._reset_templates).pack(side="left", padx=8)
        ttk.Label(tbtns, text="Changes take effect immediately for new sends.",
                  font=self.small_font, foreground="#666").pack(side="left", padx=12)

    def _load_settings(self):
        data = {}
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except Exception as e:
                self.log(f"[warn] Could not read settings file: {e}\n")
                data = {}

        for key, var in self.setting_vars.items():
            var.set(str(data.get(key, CONFIG.get(key, ""))))

        self.signature_text.delete("1.0", "end")
        self.signature_text.insert("1.0", data.get("signature", CONFIG.get("signature", "")))

        self._apply_settings_to_config(save=False)
        self.refresh_sender_info()

    def refresh_templates(self):
        for key, widget in self.template_widgets.items():
            value = CONFIG.get(key, "")
            if isinstance(widget, ttk.Entry):
                widget.var.set(value)
            else:
                widget.delete("1.0", "end")
                widget.insert("1.0", value)

    def _get_template_values(self):
        out = {}
        for key, widget in self.template_widgets.items():
            if isinstance(widget, ttk.Entry):
                out[key] = widget.var.get()
            else:
                out[key] = widget.get("1.0", "end").rstrip()
        return out

    def _apply_settings_to_config(self, save=True):
        for key, var in self.setting_vars.items():
            value = var.get().strip()
            if key == "delay_seconds":
                try: CONFIG[key] = float(value) if "." in value else int(value)
                except ValueError: CONFIG[key] = 2
            else:
                CONFIG[key] = value
        CONFIG["signature"] = self.signature_text.get("1.0", "end").strip()
        if save:
            self._write_settings()

    def _save_settings(self):
        self._apply_settings_to_config(save=False)
        # merge templates too
        CONFIG.update(self._get_template_values())
        self._write_settings()
        self.refresh_sender_info()
        self.set_status("Settings saved")
        self.log("[settings] Saved to outreach_ui_settings.json\n")
        messagebox.showinfo("Saved",
            f"Settings saved and applied.\n\n"
            f"FROM:     {self.from_addr_var.get()}\n"
            f"REPLY-TO: {self.replyto_addr_var.get()}")

    def _save_templates(self):
        CONFIG.update(self._get_template_values())
        self._write_settings()
        self.set_status("Templates saved")
        self.log("[templates] Saved to outreach_ui_settings.json\n")
        messagebox.showinfo("Saved", "Templates saved and applied.")

    def _reset_templates(self):
        if not messagebox.askyesno("Reset templates",
            "Reset all 4 templates back to their original defaults?"):
            return
        CONFIG["initial_subject"] = "Quick question about {company}"
        CONFIG["initial_body"] = """Hi {first_name},

{saw_your_company_line}

{pain_line}

I'm researching how GTM teams at companies like yours handle this. Not selling anything, just trying to understand the space better.

Would you be open to a 15-min chat sometime? Happy to share what I'm learning from others in the same boat.

{signature}"""
        CONFIG["followup_subject"] = "Re: Quick question about {company}"
        CONFIG["followup_body"] = """Hi {first_name},

Just bumping this in case it got buried.

No agenda, genuinely just curious how you're handling this.

If now isn't a good time, no worries at all. Just reply "not now" and I'll leave you alone.

{signature}"""
        self.refresh_templates()
        self._write_settings()
        self.log("[templates] Reset to defaults\n")

    def _write_settings(self):
        payload = {k: CONFIG.get(k) for k in self.setting_vars}
        payload["signature"] = CONFIG["signature"]
        for k in TEMPLATE_KEYS:
            payload[k] = CONFIG.get(k, "")
        try:
            SETTINGS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # ========================================================
    # Task runner / console
    # ========================================================
    def _run(self, fn, on_done=None, confirm=None):
        if self.busy:
            messagebox.showinfo("Please wait", "A task is already running."); return
        if confirm and not messagebox.askyesno("Confirm", confirm): return
        self.busy = True
        self.set_status("Working…")
        self._set_busy_buttons("disabled")

        def worker():
            old = sys.stdout
            sys.stdout = _QueueWriter(self.queue)
            try: fn()
            except Exception as e:
                self.queue.put(f"\n[ERROR] {e}\n")
            finally:
                sys.stdout = old
                self.queue.put(("__DONE__", on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__DONE__":
                    self.busy = False
                    self.set_status("Ready")
                    self._set_busy_buttons("normal")
                    cb = item[1]
                    if cb:
                        try: cb()
                        except Exception as e:
                            self.log(f"[ERROR refreshing views] {e}\n")
                else:
                    self.log(item)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def _set_busy_buttons(self, state):
        for btn in self._busy_buttons:
            try: btn.config(state=state)
            except tk.TclError: pass

    def log(self, text):
        self.console.config(state="normal")
        self.console.insert("end", text)
        self.console.see("end")
        self.console.config(state="disabled")

    def _clear_console(self):
        self.console.config(state="normal")
        self.console.delete("1.0", "end")
        self.console.config(state="disabled")

    def set_status(self, text): self.status_var.set(text)

    def refresh_all(self):
        try:
            self.refresh_dashboard()
            self.refresh_prospects()
            self.refresh_suppression()
            self.refresh_sender_info()
            self._refresh_compose_list()
        except Exception as e:
            self.log(f"[ERROR refreshing] {e}\n")

    def _on_close(self):
        if self.busy:
            if not messagebox.askyesno("Task running", "A task is still running. Quit anyway?"):
                return
        self.root.destroy()


# ============================================================
# Entry point
# ============================================================

def main():
    root = tk.Tk()
    app = OutreachUI(root)
    app.log("InferReach Outreach UI ready.\n")
    app.log(f"  FROM:     {app.from_addr_var.get()}\n")
    app.log(f"  REPLY-TO: {app.replyto_addr_var.get()}\n")
    app.log("Tip: edit templates in Settings → Templates, "
            "or compose custom emails per prospect in the Compose tab.\n")
    root.mainloop()


if __name__ == "__main__":
    main()