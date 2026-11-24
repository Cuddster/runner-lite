#!/usr/bin/env python3
"""
monitor.py — RunnerBot standalone monitoring & simulation GUI
=============================================================

A dependency-free (tkinter only) desktop tool to monitor RunnerBot's
runner_data.json, predict what the bot's `check_expiry` task will do, and
surface data anomalies BEFORE they cause problems in production.

What it does
------------
* Reads runner_data.json (auto-detects nested {"runners": {...}} or flat layout).
* Shows a colour-coded table of every runner's current state.
* Predicts each runner's next bot action (warn / revoke / none) and when.
* Simulates the very next check_expiry run AND the next 7 days of events,
  mirroring the live bot logic exactly: check_expiry is REVOKE-ONLY.
  access_expires is the single source of truth, set at approval time via
  extend_access_expiry(). No credit consumption, no renewal loop.
* "Weeks remaining" derived from access_expires (matching embeds.py).
* Highlights anomalies with severity (error / warning / notice).
* SSH Expiry Audit: reads live Railway data and flags non-Sunday / non-midnight
  expiry values.
* Auto-refreshes every 30 seconds (toggleable) with a live countdown.

This is a READ-ONLY tool. It never writes to runner_data.json.

Usage
-----
    python monitor.py                      # uses default data path
    python monitor.py /path/to/runner_data.json
    python monitor.py --self-test          # headless logic check (no GUI)

    # LIVE mode — read straight from the deployed bot's HTTP API:
    python monitor.py --url https://your-app.up.railway.app/api/runner-data \\
                      --api-key YOUR_KEY
    # (the API key may also come from the MONITOR_API_KEY env var)

The logic mirrors tasks.py::check_expiry as of RunnerBot v4.0 (revoke-only).
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

# ── CONSTANTS (mirror tasks.py) ──────────────────────────────────────────────
CHECK_INTERVAL_MIN = 30          # check_expiry runs every 30 minutes
RENEW_DAYS = 7                   # 1 credit = 7 days of access
WARN_LOW, WARN_HIGH = 23, 24     # 24h warning window: 23 < hours_left <= 24
FORECAST_DAYS = 7                # horizon for the multi-day forecast
REFRESH_SECONDS = 30             # GUI auto-refresh cadence
DEFAULT_SSH_COMMAND = "railway ssh -- cat /data/runner_data.json"

# ── DEFAULT DATA PATH RESOLUTION ─────────────────────────────────────────────
def default_data_path():
    """Pick the most likely runner_data.json location."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "runner_data.json"),   # alongside the bot code
        "/data/runner_data.json",                  # Railway volume mount
        os.path.join(os.getcwd(), "runner_data.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]  # default even if missing (user can browse)


# ── TIME HELPERS ─────────────────────────────────────────────────────────────
def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(value):
    """Parse an ISO timestamp into an aware UTC datetime. Returns None on failure.

    The bot stores naive ISO strings and treats them as UTC, so we replicate
    that: naive -> assume UTC; aware -> convert to UTC.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_dt(dt):
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def fmt_delta(dt, ref=None):
    """Human-friendly 'in 3d 4h' / '2h ago' relative to ref (default now)."""
    if dt is None:
        return "—"
    ref = ref or now_utc()
    secs = (dt - ref).total_seconds()
    past = secs < 0
    secs = abs(secs)
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    mins = int((secs % 3600) // 60)
    if days > 0:
        core = f"{days}d {hours}h"
    elif hours > 0:
        core = f"{hours}h {mins}m"
    else:
        core = f"{mins}m"
    return f"{core} ago" if past else f"in {core}"


# ── DATA LOADING / NORMALIZATION ─────────────────────────────────────────────
def _normalize_runners(raw):
    """Turn parsed JSON into (runners_dict, error_str).

    Handles both the real nested layout {"runners": {id: {...}}} and a flat
    {id: {...}} layout. runners_dict maps user_id(str) -> runner dict.
    """
    if not isinstance(raw, dict):
        return {}, "Top-level JSON is not an object."

    if "runners" in raw and isinstance(raw["runners"], dict):
        runners = raw["runners"]
    else:
        # Assume flat: keep only dict-valued entries that look like runners
        runners = {k: v for k, v in raw.items() if isinstance(v, dict)}

    # Keep only well-formed entries
    clean = {str(k): v for k, v in runners.items() if isinstance(v, dict)}
    return clean, None


def load_runners(path):
    """Load runner_data.json from a local file. Returns (runners_dict, action_log, error)."""
    if not os.path.exists(path):
        return {}, [], f"File not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        return {}, [], f"Invalid JSON: {e}"
    except OSError as e:
        return {}, [], f"Could not read file: {e}"
    runners, err = _normalize_runners(raw)
    return runners, [], err


def fetch_runners_from_url(url, api_key, timeout=10):
    """Fetch live runner data from the RunnerBot HTTP API.

    Sends `Authorization: Bearer <api_key>`. Returns (runners_dict, action_log, error_str).
    Uses only the stdlib (urllib) so the monitor stays dependency-free.
    """
    import urllib.request
    import urllib.error

    if not url:
        return {}, [], "No URL provided."
    if not api_key:
        return {}, [], "No API key provided (set --api-key or MONITOR_API_KEY)."

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {}, [], "Unauthorized (401) — check your API key."
        if e.code == 503:
            return {}, [], "API disabled (503) — set MONITOR_API_KEY on the bot."
        return {}, [], f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return {}, [], f"Connection failed: {e.reason}"
    except Exception as e:
        return {}, [], f"Request error: {type(e).__name__}: {e}"

    try:
        raw = json.loads(body)
    except json.JSONDecodeError as e:
        return {}, [], f"Invalid JSON from server: {e}"

    if isinstance(raw, dict) and "error" in raw and "runners" not in raw:
        return {}, [], f"Server error: {raw['error']}"

    action_log = raw.get("action_log", []) if isinstance(raw, dict) else []
    if not isinstance(action_log, list):
        action_log = []

    runners, err = _normalize_runners(raw)
    return runners, action_log, err


def is_url(source):
    """True if the source string is an HTTP(S) URL."""
    return isinstance(source, str) and source.lower().startswith(("http://", "https://"))


def get_runners(source, api_key=None):
    """Dispatch to URL or file loader based on the source string."""
    if is_url(source):
        return fetch_runners_from_url(source, api_key)
    return load_runners(source)


def fetch_runners_from_ssh(command=None, timeout=25):
    """Read live runner_data.json through Railway SSH. Read-only."""
    command = command or os.environ.get("RUNNERBOT_SSH_COMMAND") or DEFAULT_SSH_COMMAND
    try:
        output = subprocess.check_output(
            command,
            shell=True,
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {}, f"SSH command timed out after {timeout}s."
    except subprocess.CalledProcessError as e:
        return {}, f"SSH command failed: {e.output.strip() or e}"
    except Exception as e:
        return {}, f"SSH error: {type(e).__name__}: {e}"

    try:
        raw = json.loads(output)
    except json.JSONDecodeError as e:
        return {}, f"SSH returned invalid JSON: {e}"

    runners, err = _normalize_runners(raw)
    return runners, err


def runner_name(key, r):
    return (
        r.get("torn_name")
        or r.get("discord_name")
        or r.get("name")
        or f"[{key}]"
    )


def expiry_anchor_issues(expiry):
    """Return display issues for access_expires anchoring."""
    issues = []
    if expiry is None:
        return issues
    if expiry.weekday() != 6:
        issues.append("expiry not Sunday")
    if (expiry.hour, expiry.minute, expiry.second, expiry.microsecond) != (0, 0, 0, 0):
        issues.append("expiry not 00:00 UTC")
    return issues


def build_expiry_audit_lines(runners):
    lines = []
    for key, r in runners.items():
        expiry = parse_dt(r.get("access_expires"))
        issues = expiry_anchor_issues(expiry)
        access = "yes" if r.get("armory_access") else "no"
        scheduled = r.get("access_scheduled_for") or "-"
        granted = r.get("access_granted_at") or "-"
        issue_text = "; ".join(issues) if issues else "OK"
        lines.append({
            "name": runner_name(key, r),
            "torn_id": r.get("torn_id", "-"),
            "access": access,
            "expiry": fmt_dt(expiry),
            "scheduled": scheduled,
            "granted": granted,
            "issue": issue_text,
            "bad": bool(issues),
        })
    lines.sort(key=lambda x: (not x["bad"], x["name"].lower()))
    return lines


# ── CORE SIMULATION (mirrors tasks.py::check_expiry — REVOKE-ONLY) ───────────
def weeks_remaining_from_expiry(r, ref=None):
    """Derive display weeks from access_expires, matching embeds.py logic."""
    expiry = parse_dt(r.get("access_expires"))
    if expiry is None:
        return 0
    ref = ref or now_utc()
    if expiry <= ref:
        return 0
    import math
    return math.ceil((expiry - ref).total_seconds() / (7 * 86400))


def simulate_runner_events(key, r, now, horizon_days=FORECAST_DAYS):
    """Replay check_expiry forward in time for a single runner.

    Returns a list of event dicts: {time, kind, detail, weeks_after}.
    kind is one of: 'warn', 'revoke'.

    Mirrors the bot exactly (v4.0 revoke-only):
      * only runners with armory_access AND access_expires are processed
      * warning fires once when 23 < hours_left <= 24 (alerts on, not yet sent)
      * at/after expiry: revoke (terminal) — no credit consumption, no renewal
    """
    events = []
    if not r.get("armory_access"):
        return events
    expiry = parse_dt(r.get("access_expires"))
    if expiry is None:
        return events

    horizon_end = now + timedelta(days=horizon_days)
    alerts = bool(r.get("expiry_alerts", False))
    warned = bool(r.get("expiry_warning_sent", False))

    # 24h warning DM (fires once before expiry)
    warn_time = expiry - timedelta(hours=24)
    if alerts and not warned and now <= warn_time <= horizon_end:
        events.append({
            "time": warn_time,
            "kind": "warn",
            "detail": "24h expiry warning DM",
            "weeks_after": weeks_remaining_from_expiry(r, warn_time),
        })

    # Revoke at expiry
    if expiry <= horizon_end:
        events.append({
            "time": expiry,
            "kind": "revoke",
            "detail": "access expired → revoked",
            "weeks_after": 0,
        })

    return [e for e in events if e["time"] <= horizon_end]


def next_action_for_runner(key, r, now):
    """Return (kind, when_dt, detail) for the runner's NEXT bot action, or
    ('none', None, reason)."""
    if not r.get("armory_access"):
        return ("none", None, "no armory access")
    expiry = parse_dt(r.get("access_expires"))
    if expiry is None:
        return ("none", None, "no expiry set")
    events = simulate_runner_events(key, r, now, horizon_days=3650)  # long horizon
    if not events:
        return ("none", None, "no upcoming action")
    # earliest event by time (overdue events may be < now)
    nxt = min(events, key=lambda e: e["time"])
    return (nxt["kind"], nxt["time"], nxt["detail"])


def simulate_next_tick(runners, now):
    """Mirror exactly ONE check_expiry pass at the next run (<= 30 min away).

    Uses `now` as an approximation of the next tick time. Returns a list of
    action dicts describing what the bot will do to each affected runner.
    check_expiry is REVOKE-ONLY: no credit consumption, no renewal.
    """
    actions = []
    for key, r in runners.items():
        if not r.get("armory_access") or not r.get("access_expires"):
            continue
        expiry = parse_dt(r.get("access_expires"))
        if expiry is None:
            continue
        hours_left = (expiry - now).total_seconds() / 3600.0
        name = runner_name(key, r)

        # warning
        if (WARN_LOW < hours_left <= WARN_HIGH
                and r.get("expiry_alerts", False)
                and not r.get("expiry_warning_sent", False)):
            actions.append({
                "name": name, "kind": "warn",
                "detail": f"send 24h warning DM ({hours_left:.1f}h left)",
            })

        # expiry processing — revoke only
        if now >= expiry:
            actions.append({
                "name": name, "kind": "revoke",
                "detail": "access expired → revoke + DM runner/dealers",
            })
    return actions


# ── ANOMALY DETECTION ────────────────────────────────────────────────────────
def detect_anomalies(key, r, now):
    """Return a list of (severity, message). severity in error/warning/notice."""
    issues = []
    name = runner_name(key, r)
    access = bool(r.get("armory_access"))
    expiry_raw = r.get("access_expires")
    expiry = parse_dt(expiry_raw)
    weeks = weeks_remaining_from_expiry(r, now)

    if expiry_raw not in (None, "") and expiry is None:
        issues.append(("error", f"unparseable access_expires ({expiry_raw!r})"))

    for msg in expiry_anchor_issues(expiry):
        issues.append(("warning", msg))

    # access vs expiry consistency
    if access and expiry is None:
        issues.append(("error", "armory_access=True but no valid access_expires"))

    if access and expiry is not None and expiry <= now:
        issues.append(("error",
                       f"overdue by {fmt_delta(expiry, now).replace(' ago','')} "
                       f"— should already be revoked"))

    # stale expiry on inactive runner (no access, no schedule, but expiry still set)
    if not access and expiry is not None and not r.get("access_scheduled_for"):
        if expiry <= now:
            issues.append(("notice", "stale access_expires on inactive runner (already past)"))

    # scheduled but already active
    if r.get("access_scheduled_for") and access:
        issues.append(("notice", "has access_scheduled_for while access already active"))

    # access active, expiry in the future — show weeks remaining as info
    if access and expiry is not None and expiry > now and weeks <= 1:
        issues.append(("notice",
                       f"final week — access will be revoked at expiry ({fmt_dt(expiry)})"))

    return issues


def _safe_int(v):
    try:
        if isinstance(v, bool):
            return 0
        return int(v)
    except (ValueError, TypeError):
        return 0


# ── ANALYSIS AGGREGATION ─────────────────────────────────────────────────────
def analyze(runners, now):
    """Build per-runner rows + global forecast + summary."""
    rows = []
    all_events = []
    for key, r in runners.items():
        kind, when, detail = next_action_for_runner(key, r, now)
        anomalies = detect_anomalies(key, r, now)
        severity = _max_severity(anomalies)
        expiry = parse_dt(r.get("access_expires"))
        hours_left = (expiry - now).total_seconds() / 3600.0 if expiry else None
        weeks = weeks_remaining_from_expiry(r, now)
        rows.append({
            "key": key,
            "name": runner_name(key, r),
            "torn_id": r.get("torn_id", "—"),
            "weeks": weeks,
            "access": bool(r.get("armory_access")),
            "expiry": expiry,
            "hours_left": hours_left,
            "next_kind": kind,
            "next_when": when,
            "next_detail": detail,
            "anomalies": anomalies,
            "severity": severity,
        })
        for e in simulate_runner_events(key, r, now, FORECAST_DAYS):
            ev = dict(e)
            ev["name"] = runner_name(key, r)
            all_events.append(ev)

    all_events.sort(key=lambda e: e["time"])
    rows.sort(key=lambda x: (
        _severity_rank(x["severity"]),
        x["hours_left"] if x["hours_left"] is not None else float("inf"),
    ))

    summary = {
        "total": len(rows),
        "active": sum(1 for x in rows if x["access"]),
        "errors": sum(1 for x in rows if x["severity"] == "error"),
        "warnings": sum(1 for x in rows if x["severity"] == "warning"),
        "revoke_7d": sum(1 for e in all_events if e["kind"] == "revoke"),
        "warn_7d": sum(1 for e in all_events if e["kind"] == "warn"),
    }
    return rows, all_events, summary


def _max_severity(anomalies):
    rank = {"error": 3, "warning": 2, "notice": 1}
    best = ("ok", 0)
    for sev, _ in anomalies:
        if rank.get(sev, 0) > best[1]:
            best = (sev, rank[sev])
    return best[0]


def _severity_rank(sev):
    return {"error": 0, "warning": 1, "notice": 2, "ok": 3}.get(sev, 3)


# ─────────────────────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────────────────────
def launch_gui(data_path, api_key=None):
    import tkinter as tk
    from tkinter import ttk, filedialog

    # palette
    C_BG = "#1e1f26"
    C_PANEL = "#2a2c36"
    C_TEXT = "#e6e6e6"
    C_MUTED = "#9aa0b0"
    C_ERR = "#ff5c5c"
    C_WARN = "#ffb74d"
    C_NOTICE = "#ffd54f"
    C_OK = "#66bb6a"
    C_ACCENT = "#5b8def"
    ROW_ERR = "#4a2326"
    ROW_WARN = "#4a3a1f"
    ROW_NOTICE = "#403a1c"
    ROW_OK = "#23303a"

    KIND_COLOR = {
        "warn": C_NOTICE, "renew": C_OK, "revoke": C_ERR, "none": C_MUTED,
    }

    state = {"path": data_path, "api_key": api_key or "", "auto": True,
             "countdown": REFRESH_SECONDS}

    root = tk.Tk()
    root.title("RunnerBot Monitor — check_expiry simulator")
    root.geometry("1180x760")
    root.configure(bg=C_BG)

    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Treeview",
                    background=C_PANEL, fieldbackground=C_PANEL,
                    foreground=C_TEXT, rowheight=26, borderwidth=0)
    style.configure("Treeview.Heading",
                    background="#33353f", foreground=C_TEXT, relief="flat")
    style.map("Treeview.Heading", background=[("active", "#3d4150")])

    # ── header / controls ──
    header = tk.Frame(root, bg=C_BG)
    header.pack(fill="x", padx=12, pady=(10, 4))

    tk.Label(header, text="🩺 RunnerBot Monitor", bg=C_BG, fg=C_ACCENT,
             font=("Segoe UI", 15, "bold")).pack(side="left")

    status_lbl = tk.Label(header, text="", bg=C_BG, fg=C_MUTED,
                          font=("Segoe UI", 9))
    status_lbl.pack(side="right")

    # source row (accepts a local file path OR an http(s):// API URL)
    pathrow = tk.Frame(root, bg=C_BG)
    pathrow.pack(fill="x", padx=12, pady=(0, 6))
    tk.Label(pathrow, text="Source (file or URL):", bg=C_BG, fg=C_MUTED,
             font=("Segoe UI", 9)).pack(side="left")
    path_var = tk.StringVar(value=state["path"])
    path_entry = tk.Entry(pathrow, textvariable=path_var, width=60,
                          bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                          relief="flat")
    path_entry.pack(side="left", padx=6, ipady=3)

    def browse():
        p = filedialog.askopenfilename(
            title="Select runner_data.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if p:
            path_var.set(p)
            state["path"] = p
            refresh()

    def do_reload():
        state["path"] = path_var.get().strip()
        state["api_key"] = key_var.get().strip()
        refresh()

    def do_ssh_audit():
        ssh_btn.config(state="disabled", text="SSH...")
        root.update_idletasks()
        runners, err = fetch_runners_from_ssh()
        actions_box.delete("1.0", "end")
        if err:
            actions_box.insert("end", f"SSH expiry audit failed: {err}\n", "revoke")
            ssh_btn.config(state="normal", text="SSH Expiry Audit")
            return

        audit_rows = build_expiry_audit_lines(runners)
        bad_count = sum(1 for row in audit_rows if row["bad"])
        actions_box.insert(
            "end",
            f"SSH expiry audit: {len(audit_rows)} runners, {bad_count} issue(s)\n",
            "head",
        )
        for row in audit_rows[:40]:
            tag = "revoke" if row["bad"] else "muted"
            actions_box.insert("end", f"{row['name']} ({row['torn_id']})  ", "head")
            actions_box.insert("end", f"access={row['access']}  ", "muted")
            actions_box.insert("end", f"expires={row['expiry']}  ", tag)
            actions_box.insert("end", f"{row['issue']}\n", tag)
        ssh_btn.config(state="normal", text="SSH Expiry Audit")

    tk.Button(pathrow, text="Browse…", command=browse, bg=C_PANEL,
              fg=C_TEXT, relief="flat", padx=10).pack(side="left", padx=3)
    tk.Button(pathrow, text="↻ Reload", command=do_reload, bg=C_ACCENT,
              fg="white", relief="flat", padx=12).pack(side="left", padx=3)

    ssh_btn = tk.Button(pathrow, text="SSH Expiry Audit", command=do_ssh_audit,
                        bg=C_PANEL, fg=C_TEXT, relief="flat", padx=10)
    ssh_btn.pack(side="left", padx=3)

    # API key field (only relevant for URL mode; masked input)
    tk.Label(pathrow, text="API key:", bg=C_BG, fg=C_MUTED,
             font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))
    key_var = tk.StringVar(value=state["api_key"])
    key_entry = tk.Entry(pathrow, textvariable=key_var, width=16, show="•",
                         bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                         relief="flat")
    key_entry.pack(side="left", padx=6, ipady=3)

    auto_var = tk.BooleanVar(value=True)

    def toggle_auto():
        state["auto"] = auto_var.get()
        state["countdown"] = REFRESH_SECONDS

    tk.Checkbutton(pathrow, text="Auto-refresh (30s)", variable=auto_var,
                   command=toggle_auto, bg=C_BG, fg=C_TEXT,
                   selectcolor=C_PANEL, activebackground=C_BG,
                   activeforeground=C_TEXT,
                   font=("Segoe UI", 9)).pack(side="left", padx=10)

    # ── summary bar ──
    summary_lbl = tk.Label(root, text="", bg=C_BG, fg=C_TEXT,
                           font=("Consolas", 10), anchor="w", justify="left")
    summary_lbl.pack(fill="x", padx=12, pady=(0, 4))

    # ── table ──
    table_frame = tk.Frame(root, bg=C_BG)
    table_frame.pack(fill="both", expand=True, padx=12, pady=4)

    cols = ("name", "torn", "weeks", "access", "expiry",
            "left", "next", "when", "status")
    headers = {
        "name": "Runner", "torn": "Torn ID", "weeks": "Weeks",
        "access": "Access", "expiry": "Expires", "left": "Time Left",
        "next": "Next Action", "when": "When", "status": "Status / Anomaly",
    }
    widths = {
        "name": 150, "torn": 70, "weeks": 60, "access": 70, "expiry": 150,
        "left": 95, "next": 90, "when": 110, "status": 300,
    }
    tree = ttk.Treeview(table_frame, columns=cols, show="headings",
                        selectmode="browse")
    for c in cols:
        tree.heading(c, text=headers[c])
        anchor = "w" if c in ("name", "status") else "center"
        tree.column(c, width=widths[c], anchor=anchor, stretch=(c == "status"))
    vsb = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    tree.tag_configure("error", background=ROW_ERR, foreground="#ffd7d7")
    tree.tag_configure("warning", background=ROW_WARN, foreground="#ffe7c2")
    tree.tag_configure("notice", background=ROW_NOTICE, foreground="#fff2c2")
    tree.tag_configure("ok", background=ROW_OK, foreground=C_TEXT)

    # detail popup on select
    detail_var = tk.StringVar(value="Select a row to see full details.")
    detail_lbl = tk.Label(root, textvariable=detail_var, bg=C_PANEL,
                          fg=C_MUTED, font=("Consolas", 9), anchor="w",
                          justify="left", padx=8, pady=4)
    detail_lbl.pack(fill="x", padx=12, pady=(2, 4))

    row_lookup = {}

    def on_select(_evt=None):
        sel = tree.selection()
        if not sel:
            return
        row = row_lookup.get(sel[0])
        if not row:
            return
        parts = [f"{row['name']}  (id {row['key']})"]
        parts.append(f"next: {row['next_kind']} {fmt_delta(row['next_when'])} "
                     f"— {row['next_detail']}")
        if row["anomalies"]:
            for sev, msg in row["anomalies"]:
                parts.append(f"  [{sev.upper()}] {msg}")
        else:
            parts.append("  no anomalies")
        detail_var.set("   ".join(parts[:2]) + "\n" + "\n".join(parts[2:]))

    tree.bind("<<TreeviewSelect>>", on_select)

    # ── bottom: two simulation panels ──
    bottom = tk.Frame(root, bg=C_BG)
    bottom.pack(fill="both", expand=False, padx=12, pady=(2, 10))

    left_panel = tk.Frame(bottom, bg=C_PANEL)
    left_panel.pack(side="left", fill="both", expand=True, padx=(0, 6))
    tk.Label(left_panel, text="⏱ Next check_expiry run (≤30 min)", bg=C_PANEL,
             fg=C_ACCENT, font=("Segoe UI", 10, "bold"),
             anchor="w").pack(fill="x", padx=8, pady=(6, 2))
    next_tick_box = tk.Text(left_panel, height=9, bg="#23252e", fg=C_TEXT,
                            relief="flat", font=("Consolas", 9), wrap="word")
    next_tick_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    right_panel = tk.Frame(bottom, bg=C_PANEL)
    right_panel.pack(side="left", fill="both", expand=True, padx=(6, 6))
    tk.Label(right_panel, text="📅 7-day forecast (chronological)", bg=C_PANEL,
             fg=C_ACCENT, font=("Segoe UI", 10, "bold"),
             anchor="w").pack(fill="x", padx=8, pady=(6, 2))
    forecast_box = tk.Text(right_panel, height=9, bg="#23252e", fg=C_TEXT,
                           relief="flat", font=("Consolas", 9), wrap="word")
    forecast_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    actions_panel = tk.Frame(bottom, bg=C_PANEL)
    actions_panel.pack(side="left", fill="both", expand=True, padx=(6, 0))
    tk.Label(actions_panel, text="🔔 Recent actions (live)", bg=C_PANEL,
             fg=C_ACCENT, font=("Segoe UI", 10, "bold"),
             anchor="w").pack(fill="x", padx=8, pady=(6, 2))
    actions_box = tk.Text(actions_panel, height=9, bg="#23252e", fg=C_TEXT,
                          relief="flat", font=("Consolas", 9), wrap="word")
    actions_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    for box in (next_tick_box, forecast_box, actions_box):
        box.tag_configure("warn", foreground=C_NOTICE)
        box.tag_configure("renew", foreground=C_OK)
        box.tag_configure("revoke", foreground=C_ERR)
        box.tag_configure("grant", foreground=C_ACCENT)
        box.tag_configure("muted", foreground=C_MUTED)
        box.tag_configure("head", foreground=C_ACCENT)

    # ── refresh logic ──
    def refresh():
        now = now_utc()
        src = state["path"]
        live = is_url(src)
        runners, action_log, err = get_runners(src, state["api_key"])

        for iid in tree.get_children():
            tree.delete(iid)
        row_lookup.clear()

        mode_tag = "🌐 LIVE" if live else "📄 file"
        if err:
            summary_lbl.config(text=f"⚠ {err}", fg=C_ERR)
            next_tick_box.delete("1.0", "end")
            forecast_box.delete("1.0", "end")
            actions_box.delete("1.0", "end")
            status_lbl.config(
                text=f"{mode_tag} · load failed · {now.strftime('%H:%M:%S')} UTC")
            return

        rows, events, summary = analyze(runners, now)

        # summary bar
        summary_lbl.config(
            text=(f"Runners: {summary['total']}   "
                  f"Active: {summary['active']}   "
                  f"🔴 Errors: {summary['errors']}   "
                  f"🟠 Warnings: {summary['warnings']}   "
                  f"|  Next 7d →  revoke: {summary['revoke_7d']}   "
                  f"warn DMs: {summary['warn_7d']}"),
            fg=C_TEXT)

        # table rows
        for row in rows:
            tag = row["severity"] if row["severity"] != "ok" else "ok"
            anomaly_txt = "; ".join(m for _, m in row["anomalies"]) or "OK"
            left = (f"{row['hours_left']:.1f}h"
                    if row["hours_left"] is not None else "—")
            iid = tree.insert("", "end", values=(
                row["name"],
                row["torn_id"],
                row["weeks"],
                "✅" if row["access"] else "—",
                fmt_dt(row["expiry"]),
                left,
                row["next_kind"],
                fmt_delta(row["next_when"]) if row["next_when"] else "—",
                anomaly_txt,
            ), tags=(tag,))
            row_lookup[iid] = row

        # next-tick panel
        next_tick_box.delete("1.0", "end")
        tick_actions = simulate_next_tick(runners, now)
        if not tick_actions:
            next_tick_box.insert("end", "No actions — bot will idle this run.\n",
                                 "muted")
        else:
            for a in tick_actions:
                next_tick_box.insert("end", f"• {a['name']}: ", "head")
                next_tick_box.insert("end", f"{a['kind'].upper()} ", a["kind"])
                next_tick_box.insert("end", f"— {a['detail']}\n")

        # forecast panel
        forecast_box.delete("1.0", "end")
        if not events:
            forecast_box.insert("end", "No predicted events in the next "
                                f"{FORECAST_DAYS} days.\n", "muted")
        else:
            for e in events:
                stamp = e["time"].strftime("%a %m/%d %H:%M")
                rel = fmt_delta(e["time"], now)
                forecast_box.insert("end", f"{stamp} ({rel})  ", "muted")
                forecast_box.insert("end", f"{e['kind'].upper()} ", e["kind"])
                forecast_box.insert("end", f"{e['name']}: {e['detail']}\n")

        # recent actions panel (live action_log from the bot)
        actions_box.delete("1.0", "end")
        if not action_log:
            actions_box.insert("end", "No actions logged yet.\n", "muted")
        else:
            recent = list(reversed(action_log))[:30]
            for a in recent:
                t = parse_dt(a.get("time"))
                stamp = t.strftime("%a %m/%d %H:%M") if t else "—"
                rel = fmt_delta(t, now) if t else ""
                actions_box.insert("end", f"{stamp} ({rel})  ", "muted")
                actions_box.insert("end", f"{str(a.get('kind', '?')).upper()} ", a.get("kind", ""))
                actions_box.insert("end", f"{a.get('name', '?')}: {a.get('detail', '')}\n")

        status_lbl.config(
            text=f"{mode_tag} · updated {now.strftime('%H:%M:%S')} UTC · "
                 f"{summary['total']} runners")

    # ── auto-refresh tick (1s cadence for countdown) ──
    def tick():
        if state["auto"]:
            state["countdown"] -= 1
            if state["countdown"] <= 0:
                refresh()
                state["countdown"] = REFRESH_SECONDS
            cur = status_lbl.cget("text").split(" · next")[0]
            status_lbl.config(text=f"{cur} · next in {state['countdown']}s")
        root.after(1000, tick)

    refresh()
    root.after(1000, tick)
    root.mainloop()


# ── HEADLESS SELF-TEST ───────────────────────────────────────────────────────
def self_test():
    """Exercise the simulation logic without a GUI (revoke-only model)."""
    now = datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)
    sample = {
        "runners": {
            "1001": {  # healthy, expires in 3 days, alerts on
                "torn_id": 1, "torn_name": "Healthy", "credits": 0,
                "armory_access": True, "expiry_alerts": True,
                "expiry_warning_sent": False,
                "access_expires": (now + timedelta(days=3)).isoformat(),
            },
            "1002": {  # overdue — should already be revoked
                "torn_id": 2, "torn_name": "Overdue", "credits": 0,
                "armory_access": True,
                "access_expires": (now - timedelta(hours=2)).isoformat(),
            },
            "1003": {  # inactive, no access — no action expected
                "torn_id": 3, "torn_name": "Inactive", "credits": 0,
                "armory_access": False, "access_expires": None,
            },
            "1004": {  # long runway: expires in 21 days (3 weeks banked)
                "torn_id": 4, "torn_name": "Banked", "credits": 0,
                "armory_access": True, "expiry_alerts": False,
                "access_expires": (now + timedelta(days=21)).isoformat(),
            },
            "1005": {  # final week: access active, expires in 2 days
                "torn_id": 5, "torn_name": "FinalWeek", "credits": 0,
                "armory_access": True,
                "access_expires": (now + timedelta(days=2)).isoformat(),
            },
            "1006": {  # bad expiry string
                "torn_id": 6, "torn_name": "BadDate", "credits": 0,
                "armory_access": True, "access_expires": "not-a-date",
            },
            "1007": {  # non-midnight expiry (anchor issue)
                "torn_id": 7, "torn_name": "NonMidnight", "credits": 0,
                "armory_access": True,
                "access_expires": "2026-06-28T00:23:54",
            },
        }
    }
    runners = sample["runners"]
    rows, events, summary = analyze(runners, now)

    print("=== SUMMARY ===")
    print(summary)
    assert summary["total"] == 7
    assert "renew_7d" not in summary, "renew_7d should not exist in revoke-only model"

    print("\n=== NEXT-TICK ACTIONS ===")
    for a in simulate_next_tick(runners, now):
        print(f"  {a['name']}: {a['kind']} — {a['detail']}")

    print("\n=== PER-RUNNER ===")
    for row in rows:
        print(f"  {row['name']:<13} weeks={row['weeks']} "
              f"access={row['access']} next={row['next_kind']} "
              f"sev={row['severity']} :: "
              f"{'; '.join(m for _, m in row['anomalies']) or 'OK'}")

    # Assertions on key behaviors
    by_name = {r["name"]: r for r in rows}

    assert by_name["Overdue"]["next_kind"] == "revoke", "overdue must revoke"
    assert by_name["Overdue"]["severity"] == "error", "overdue is error"

    assert by_name["Inactive"]["next_kind"] == "none", "inactive has no action"

    assert by_name["BadDate"]["severity"] == "error", "bad date is error"

    assert any(s == "warning" for s, _ in by_name["NonMidnight"]["anomalies"]), \
        "non-midnight expiry should be a warning"

    assert by_name["Banked"]["weeks"] == 3, "21 days = 3 weeks"

    # Healthy: expires in 3d, alerts on → should get warn event at +2d
    healthy_events = [e for e in events if e["name"] == "Healthy"]
    kinds = [e["kind"] for e in healthy_events]
    print("\n=== Healthy 7d events ===", kinds)
    assert "warn" in kinds, "healthy should get warn DM"
    assert "revoke" in kinds, "healthy should get revoke at expiry"
    assert "renew" not in kinds, "no renew events in revoke-only model"

    # No renew events anywhere
    all_kinds = set(e["kind"] for e in events)
    assert "renew" not in all_kinds, "no renew events should exist"

    print("\n✅ ALL SELF-TESTS PASSED")
    return 0


# ── ENTRY POINT ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="RunnerBot monitoring & check_expiry simulator (tkinter GUI)")
    ap.add_argument("data_path", nargs="?", default=None,
                    help="Path to runner_data.json (default: auto-detect)")
    ap.add_argument("--url", default=None,
                    help="Fetch LIVE data from the bot's API instead of a local "
                         "file, e.g. https://your-app.up.railway.app/api/runner-data")
    ap.add_argument("--api-key", default=None,
                    help="API key for --url mode (or set MONITOR_API_KEY env var)")
    ap.add_argument("--self-test", action="store_true",
                    help="Run headless logic self-test and exit (no GUI)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    api_key = args.api_key or os.environ.get("MONITOR_API_KEY", "")

    # URL mode takes precedence over a local file path when provided.
    if args.url:
        source = args.url
    else:
        source = args.data_path or default_data_path()

    try:
        launch_gui(source, api_key=api_key)
    except Exception as e:  # tkinter unavailable / no display
        import traceback
        print("GUI failed to start:", e, file=sys.stderr)
        traceback.print_exc()
        print("\nTip: run with --self-test to verify logic without a display.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
