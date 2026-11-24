import os
import json
from datetime import datetime, timezone, timedelta
import gspread
from google.oauth2.service_account import Credentials
from data import load_data, xanax_in_week, get_week_start
from config import WEEKLY_QUOTA, REIMBURSEMENT

SHEET_ID = "18sUxJFDRvyyic5KmvHlnUPbpRXn1VmbRGXVb0m3xLt4"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NAME = "Runner Data"

HEADERS = [
    "Runner", "Torn ID", "Submission ID", "Submitted", "Xanax",
    "Status", "Approved At", "Approved By", "Reimbursed", "Reimbursed At", "Reimbursed By",
    "Rejection Reason", "Rejected At", "Rejected By",
    "API Status", "Self Reported",
    "Armory Access", "Access Granted", "Access Expires", "Credits (Total)",
    "Total Xanax (All Time)", "This Week Xanax", "Expiry Alerts", "Exported At"
]

# Colors
COLOR_DARK   = {"red": 0.17, "green": 0.18, "blue": 0.19}
COLOR_GOLD   = {"red": 0.79, "green": 0.66, "blue": 0.30}
COLOR_GREEN  = {"red": 0.30, "green": 0.69, "blue": 0.31}
COLOR_GREY   = {"red": 0.85, "green": 0.85, "blue": 0.85}
COLOR_WHITE  = {"red": 1.0,  "green": 1.0,  "blue": 1.0}


def get_client():
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise Exception("GOOGLE_CREDENTIALS environment variable not set")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def fmt_ts(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    except Exception:
        return "—"


async def export_to_sheets():
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _export_sync)


def _export_sync():
    print("[SHEETS] Export started", flush=True)
    try:
        client = get_client()
        sheet = client.open_by_key(SHEET_ID)
        data = load_data()
        runners = list(data["runners"].values())
        now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")

        # ── Runner Data sheet ──────────────────────────────────────────────
        try:
            ws = sheet.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(SHEET_NAME, rows=1000, cols=len(HEADERS))
            ws.append_row(HEADERS)
            ws.format("A1:X1", {"textFormat": {"bold": True}, "backgroundColor": COLOR_DARK})
            ws.freeze(rows=1)

        existing = ws.col_values(3)
        existing_ids = set(existing[1:])

        new_rows = []
        for r in runners:
            name = r.get("torn_name") or "Unknown"
            torn_id = str(r.get("torn_id") or "—")
            armory_access = "Active" if r.get("armory_access") else "Inactive"
            expiry = r.get("access_expires")
            exp_str = ""
            granted_str = ""
            if expiry:
                exp_dt = datetime.fromisoformat(expiry).replace(tzinfo=timezone.utc)
                exp_str = exp_dt.strftime("%b %d, %Y %H:%M UTC")
                approvals = [s for s in r.get("submissions", []) if s.get("status") == "approved" and s.get("approved_at")]
                if approvals:
                    latest = max(approvals, key=lambda x: x["approved_at"])
                    try:
                        granted_dt = datetime.fromisoformat(latest["approved_at"]).replace(tzinfo=timezone.utc)
                        granted_str = granted_dt.strftime("%b %d, %Y %H:%M UTC")
                    except Exception:
                        pass

            for s in r.get("submissions", []):
                sub_id = f"{torn_id}_{s.get('id', '')}"
                if sub_id in existing_ids:
                    continue
                try:
                    dt = datetime.fromisoformat(s["submitted_at"]).replace(tzinfo=timezone.utc)
                    submitted_str = dt.strftime("%b %d, %Y %H:%M UTC")
                except Exception:
                    submitted_str = "—"
                new_rows.append([
                    name, torn_id, sub_id, submitted_str,
                    s.get("xanax_deposited", 0),
                    s.get("status", "—").capitalize(),
                    fmt_ts(s.get("approved_at")),
                    s.get("approved_by") or "—",
                    "Yes" if s.get("reimbursed") else "No",
                    fmt_ts(s.get("reimbursed_at")),
                    s.get("reimbursed_by") or "—",
                    s.get("rejection_reason") or "—",
                    fmt_ts(s.get("rejected_at")),
                    s.get("rejected_by") or "—",
                    s.get("api_status") or "—",
                    "Yes" if s.get("self_reported") else "No",
                    armory_access, granted_str or "—", exp_str or "—",
                    r.get("credits", 0), r.get("total_xanax", 0),
                    xanax_in_week(r),
                    "On" if r.get("expiry_alerts") else "Off",
                    now,
                ])

        if new_rows:
            ws.append_rows(new_rows)
            print(f"[SHEETS] Appended {len(new_rows)} new rows", flush=True)

        # ── Weekly calendar tabs ───────────────────────────────────────────
        _build_weekly_calendars(sheet, runners)

        return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"Export failed: {e}"


def _build_weekly_calendars(sheet, runners):
    """Build one tab per week that has any approved submissions."""
    # Collect all approved submissions grouped by week start
    from collections import defaultdict
    weeks = defaultdict(list)  # week_start_dt -> list of (runner, submission)

    for r in runners:
        name = r.get("torn_name") or "Unknown"
        for s in r.get("submissions", []):
            if s.get("status") != "approved":
                continue
            try:
                dt = datetime.fromisoformat(s["submitted_at"]).replace(tzinfo=timezone.utc)
                days_since_sun = (dt.weekday() - 6) % 7
                week_start = (dt - timedelta(days=days_since_sun)).replace(hour=0, minute=0, second=0, microsecond=0)
                weeks[week_start].append((name, s, dt))
            except Exception:
                continue

    # Also include current week even if no submissions
    current_week = get_week_start(0)
    if current_week not in weeks:
        weeks[current_week] = []

    for week_start, entries in weeks.items():
        week_end = week_start + timedelta(days=6)
        tab_name = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d')}"

        # Get or create tab
        try:
            ws = sheet.worksheet(tab_name)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(tab_name, rows=20, cols=8)

        # Build calendar grid
        # Row 1: Week title
        # Row 2: Day headers (Mon Tue Wed Thu Fri Sat Sun)
        # Row 3+: Runner rows with deposit info in the correct day column

        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_dates = [week_start + timedelta(days=i) for i in range(7)]

        # Title row
        ws.update("A1", [[f"Week: {week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')} — Resilience Runner Program"]])
        ws.merge_cells("A1:H1")
        ws.format("A1", {"textFormat": {"bold": True, "fontSize": 12}, "backgroundColor": COLOR_DARK,
                          "horizontalAlignment": "CENTER"})

        # Day header row with date numbers
        header_row = ["Runner"] + [f"{d}\n{day_dates[i].strftime('%b %d')}" for i, d in enumerate(days)]
        ws.update("A2", [header_row])
        ws.format("A2:H2", {"textFormat": {"bold": True}, "backgroundColor": COLOR_DARK,
                             "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP"})

        # Build per-runner per-day deposit data
        runner_days = defaultdict(lambda: defaultdict(list))
        for name, s, dt in entries:
            day_idx = (dt.weekday() - 0) % 7  # Monday=0
            # Convert: weekday() gives Mon=0..Sun=6, which matches our columns
            runner_days[name][day_idx].append(s.get("xanax_deposited", 0))

        # Get all runner names that appear this week or are in the roster
        all_runners = set(runner_days.keys())
        for r in runners:
            all_runners.add(r.get("torn_name") or "Unknown")

        # Write runner rows
        batch_data = []
        batch_formats = []
        row_idx = 3
        for runner_name in sorted(all_runners):
            row = [runner_name]
            for day_idx in range(7):
                deposits = runner_days[runner_name][day_idx]
                if deposits:
                    total = sum(deposits)
                    row.append(f"{total}x Xanax")
                else:
                    row.append("")
            batch_data.append(row)

            # Format: gold for deposit days, white for empty
            cell_formats = []
            for col_idx in range(1, 8):
                day_idx = col_idx - 1
                deposits = runner_days[runner_name][day_idx]
                cell_formats.append({
                    "backgroundColor": COLOR_GOLD if deposits else COLOR_WHITE,
                    "horizontalAlignment": "CENTER"
                })
            batch_formats.append((row_idx, cell_formats))
            row_idx += 1

        if batch_data:
            ws.update(f"A3", batch_data)
            # Apply formats cell by cell
            requests = []
            for row_num, cell_formats in batch_formats:
                for col_num, fmt in enumerate(cell_formats, start=2):  # cols B-H
                    requests.append({
                        "repeatCell": {
                            "range": {
                                "sheetId": ws.id,
                                "startRowIndex": row_num - 1,
                                "endRowIndex": row_num,
                                "startColumnIndex": col_num - 1,
                                "endColumnIndex": col_num,
                            },
                            "cell": {"userEnteredFormat": {"backgroundColor": fmt["backgroundColor"],
                                                           "horizontalAlignment": fmt["horizontalAlignment"]}},
                            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment)"
                        }
                    })
            if requests:
                sheet.batch_update({"requests": requests})

        # Format runner name column
        ws.format(f"A3:A{row_idx}", {"textFormat": {"bold": True}})

        # Summary row
        total_row = row_idx
        ws.update(f"A{total_row}", [[f"Exported: {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}"]])
        ws.format(f"A{total_row}:H{total_row}", {"textFormat": {"italic": True},
                                                   "backgroundColor": COLOR_GREY})

        # Set column widths
        sheet.batch_update({"requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 120}, "fields": "pixelSize"
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 8},
                "properties": {"pixelSize": 110}, "fields": "pixelSize"
            }},
        ]})

        print(f"[SHEETS] Built calendar tab: {tab_name}", flush=True)
AUTOMATION_SHEET_ID = "12XDoMd1kwZki55cQCS0mf934v2Zv934hOZEpuxwQ8oU"
AUTOMATION_TAB = "Automation Test"

AUTOMATION_HEADERS = [
    "Runner Name", "Ticket ID", "Deposit Date", "Total Deposited",
    "Processed By", "Reimbursement", "Credits Earned", "Paid?",
    "Notes", "Deposit Log", "Date Submitted", "Approved By", "Approved At"
]


async def push_approval_to_sheet(runner: dict, sub: dict):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _push_approval_sync, runner, sub)


def _push_approval_sync(runner: dict, sub: dict):
    try:
        import re
        client = get_client()
        sheet = client.open_by_key(AUTOMATION_SHEET_ID)

        try:
            ws = sheet.worksheet(AUTOMATION_TAB)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(AUTOMATION_TAB, rows=1000, cols=len(AUTOMATION_HEADERS))

        first_row = ws.row_values(1)
        if first_row != AUTOMATION_HEADERS:
            ws.update("A1", [AUTOMATION_HEADERS])
            ws.format("A1:M1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)

        data = load_data()
        ticket_id = data.get("sheet_ticket_counter", 97)
        data["sheet_ticket_counter"] = ticket_id + 1
        from data import save_data
        save_data(data)
        sub["sheet_ticket_id"] = ticket_id

        submitted_at = sub.get("submitted_at", "")

        deposit_date_str = ""
        log_text = sub.get("deposit_log", "")
        log_match = re.search(r'(\d{2}:\d{2}:\d{2}) - (\d{2}/\d{2}/\d{2})', log_text)
        if log_match:
            try:
                dep_dt = datetime.strptime(f"{log_match.group(2)} {log_match.group(1)}", "%d/%m/%y %H:%M:%S")
                deposit_date_str = dep_dt.replace(tzinfo=timezone.utc).strftime("%b %d, %Y %H:%M UTC")
            except Exception:
                deposit_date_str = fmt_ts(submitted_at)
        else:
            deposit_date_str = fmt_ts(submitted_at)

        row = [
            runner.get("torn_name", "Unknown"),
            ticket_id,
            deposit_date_str,
            sub.get("xanax_deposited", 87),
            "RunnerBot",
            REIMBURSEMENT,
            sub.get("credits_earned", 1),
            "FALSE",
            sub.get("api_status", "") if sub.get("self_reported") else "",
            sub.get("deposit_log", ""),
            fmt_ts(submitted_at),
            sub.get("approved_by", ""),
            fmt_ts(sub.get("approved_at")),
        ]

        col_a = ws.col_values(1)
        next_row = len(col_a) + 1

        ws.update(f"A{next_row}", [row])
        print(f"[SHEETS] Pushed approval for {runner.get('torn_name')} ticket #{ticket_id} to row {next_row}", flush=True)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[SHEETS] Push failed: {e}", flush=True)

async def update_paid_status_in_sheet(ticket_id: int):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _update_paid_status_sync, ticket_id)


def _update_paid_status_sync(ticket_id: int):
    try:
        client = get_client()
        sheet = client.open_by_key(AUTOMATION_SHEET_ID)
        ws = sheet.worksheet(AUTOMATION_TAB)
        
        # Find row with matching ticket ID (column B)
        ticket_col = ws.col_values(2)
        for idx, val in enumerate(ticket_col, start=1):
            if str(val) == str(ticket_id):
                ws.update(f"H{idx}", [["TRUE"]])
                print(f"[SHEETS] Marked ticket #{ticket_id} as paid at row {idx}", flush=True)
                return
        print(f"[SHEETS] Ticket #{ticket_id} not found in sheet", flush=True)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[SHEETS] Update paid failed: {e}", flush=True)
