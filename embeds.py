# embeds.py — v3.0
# Changelog:
#   3.0 — Credits refactor: all display uses weeks_remaining() derived from access_expires.
#         Hides unapproved new runners. Fixed terminal runner filter. Removed dead code.
#   2.6 — build_calendar_embed, build_approved_submission_embed
#   2.1 — Runner dashboard: reimbursement owed, deadline countdown, streak, lifetime earnings, access banked
#   1.1 — All Runners active/inactive split, past week submission filter, dealer exclusion across all embeds
#   1.0 — Initial release
import calendar as _calendar
import discord
import math
from datetime import datetime, timezone, timedelta
from config import COL_GOLD, COL_GREEN, COL_RED, WEEKLY_QUOTA, REIMBURSEMENT, ACCESS_POSITION, NO_ACCESS_POSITION
from data import load_data, load_settings, xanax_in_week, fmt_week, get_week_start, weeks_remaining

def next_saturday_countdown():
    """Returns a Discord timestamp for next Saturday midnight UTC."""
    now = datetime.now(timezone.utc)
    days_until_sat = (5 - now.weekday()) % 7
    if days_until_sat == 0:
        days_until_sat = 7
    next_sat = (now + timedelta(days=days_until_sat)).replace(hour=0, minute=0, second=0, microsecond=0)
    return f"<t:{int(next_sat.timestamp())}:R>"


def calc_streak(runner):
    if not runner.get('armory_access') or not runner.get('access_granted_at'):
        return 0
    try:
        granted = datetime.fromisoformat(runner['access_granted_at']).replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    days = (now - granted).total_seconds() / 86400
    return max(1, int(days // 7))

def calc_owed(runner):
    """Calculate reimbursement owed (approved but not yet reimbursed)."""
    return sum(
        s.get("credits_earned", 0) * REIMBURSEMENT
        for s in runner.get("submissions", [])
        if s.get("status") == "approved" and not s.get("reimbursed")
    )


def calc_lifetime_earnings(runner):
    """Calculate total reimbursement paid out."""
    return sum(
        s.get("credits_earned", 0) * REIMBURSEMENT
        for s in runner.get("submissions", [])
        if s.get("status") == "approved" and s.get("reimbursed")
    )


# ── HELPERS ────────────────────────────────────────────────────────────────
def expiry_countdown(expiry_iso):
    if not expiry_iso:
        return "—"
    expiry = datetime.fromisoformat(expiry_iso).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = (expiry - now).total_seconds()
    if delta <= 0:
        return "Expired"
    hours = delta / 3600
    if hours < 24:
        return f"{int(hours)}h remaining"
    return f"{int(delta / 86400)}d remaining"


def count_pending_approval():
    data = load_data()
    return sum(1 for r in data["runners"].values() for s in r["submissions"] if s["status"] == "pending")


def count_pending_reimburse():
    data = load_data()
    return sum(1 for r in data["runners"].values() for s in r["submissions"]
               if s["status"] == "approved" and not s.get("reimbursed"))


def get_pending_submissions():
    data = load_data()
    pending = []
    for discord_id, r in data["runners"].items():
        for i, s in enumerate(r["submissions"]):
            if s["status"] == "pending":
                pending.append((discord_id, i, r, s))
    return pending

def runner_tier(total_xanax):
    if total_xanax >= 10000:
        return "🔥 Legend"
    if total_xanax >= 5000:
        return "👑 Crown"
    if total_xanax >= 2000:
        return "💎 Diamond"
    if total_xanax >= 1000:
        return "🥇 Gold"
    if total_xanax >= 500:
        return "🥈 Silver"
    return "🥉 Bronze"

# ── RUNNER EMBEDS ──────────────────────────────────────────────────────────
def build_xr_embed(runner):
    linked = runner.get("torn_id") is not None
    name = runner.get("torn_name") or "Not linked"
    tid = runner.get("torn_id")
    access = runner.get("armory_access", False)
    expiry = runner.get("access_expires")
    alerts = runner.get("expiry_alerts", False)
    total = runner.get("total_xanax", 0)
    xan_wk = xanax_in_week(runner)
    credits = weeks_remaining(runner)
    owed = calc_owed(runner)
    lifetime = calc_lifetime_earnings(runner)
    streak = calc_streak(runner)


    torn_val = f"{name} [{tid}]" if linked and name != "Unknown" else (f"[{tid}]" if linked else "Not linked")

    embed = discord.Embed(title="Xanax Runner", colour=COL_GOLD)
    embed.add_field(name="Torn Account", value=torn_val, inline=True)
    embed.add_field(name="This Week", value=f"{xan_wk} / {WEEKLY_QUOTA} Xanax", inline=True)
    embed.add_field(name="Total Deposited", value=f"{total} — {runner_tier(total)}", inline=True)
    embed.add_field(name="Access Banked", value=f"{credits} week(s)", inline=True)
    embed.add_field(name="Armory Access", value="Active" if access else "Inactive", inline=True)
    embed.add_field(name="Access Expires", value=expiry_countdown(expiry), inline=True)
    embed.add_field(name="Streak", value=f"{streak} week(s) 🔥" if streak > 0 else "0 weeks", inline=True)
    embed.add_field(name="Owed", value=f"${owed:,.0f}" if owed > 0 else "None", inline=True)
    embed.add_field(name="Lifetime Reimbursements", value=f"${lifetime:,.0f}", inline=True)
    embed.add_field(name="Next Deadline", value=next_saturday_countdown(), inline=True)
    embed.add_field(name="Expiry Alerts", value="On (24h warning)" if alerts else "Off", inline=True)

    subs = runner.get("submissions", [])
    if subs:
        history = []
        for s in reversed(subs[-5:]):
            ts = int(datetime.fromisoformat(s["submitted_at"]).timestamp())
            flag = " ⚠ MISMATCH" if s.get("log_mismatch") else ""
            reimb = "Reimbursed" if s.get("reimbursed") else ("Rejected" if s.get("status") == "rejected" else "Pending payment")
            history.append(f"<t:{ts}:D> — {s['xanax_deposited']} Xanax — {s['status'].capitalize()}{flag} — {reimb}")
        embed.add_field(name="Recent Submissions", value="\n".join(history), inline=False)

    embed.set_footer(text="Resilience — Runner Program")
    return embed


# ── LEADER EMBEDS ──────────────────────────────────────────────────────────
def build_leader_embed_dashboard():
    data = load_data()
    all_runners = {k: v for k, v in data["runners"].items() if not v.get("is_dealer") or v.get("is_runner")}
    pa = count_pending_approval()
    pr = count_pending_reimburse()

    embed = discord.Embed(title="Leadership Dashboard", colour=COL_GOLD)
    embed.add_field(name="Runners", value=str(len(all_runners)), inline=True)
    embed.add_field(name="Active Access", value=str(sum(1 for r in all_runners.values() if r.get("armory_access"))), inline=True)
    embed.add_field(name="Week Xanax", value=str(sum(xanax_in_week(r) for r in all_runners.values())), inline=True)
    embed.add_field(name="Total Xanax (all time)", value=str(sum(r.get("total_xanax", 0) for r in all_runners.values())), inline=True)
    embed.add_field(name="Pending Approval", value=f"**{pa}** submission(s) awaiting review" if pa else "None", inline=False)
    embed.add_field(name="Pending Reimbursement", value=f"**{pr}** runner(s) awaiting payment" if pr else "None", inline=False)
    g = classify_roles()
    last = load_settings().get("last_role_check")
    roles = f"✅ {len(g['tap']) + len(g['sap'])} correct · ⚠️ {len(g['mismatched'])} mismatched"
    if g["other"]:
        roles += f" · {len(g['other'])} other"
    roles += f"\nLast checked {_ago(last)}" if last else "\nNot checked yet"
    embed.add_field(name="Faction Roles", value=roles, inline=False)
    embed.set_footer(text=f"Week: {fmt_week(0)} — Resilience Runner Program")
    return embed


def build_leader_embed_all(offset=0):
    data = load_data()
    runners = {k: v for k, v in data["runners"].items() if not v.get("is_dealer") or v.get("is_runner")}
    embed = discord.Embed(title="All Runners", colour=COL_GOLD)
    embed.description = f"Week: {fmt_week(offset)}\n"
    if offset > 0:
        embed.description += "_Showing runners who submitted this week_\n\n"
    else:
        embed.description += "\n"
    
    if not runners:
        embed.description += "No runners registered."
        return embed

    week_start = get_week_start(offset)
    week_end = week_start + __import__("datetime").timedelta(weeks=1)

    active_lines = []
    inactive_lines = []
    pending_lines = []
    pending_ids = {str(d) for d, _ in get_pending_access_runners()} if offset == 0 else set()

    for r in runners.values():
        name = r.get("torn_name") or f"[{r['discord_id']}]"
        # Approved but not yet granted — show under Pending (current week only)
        if str(r.get("discord_id")) in pending_ids:
            sched = r.get("access_scheduled_for")
            if sched:
                try:
                    when = f"⏰ <t:{int(datetime.fromisoformat(sched).timestamp())}:R>"
                except Exception:
                    when = "⏰ scheduled"
            else:
                when = "⚠️ not scheduled"
            pending_lines.append(f"**{name}** — {when}")
            continue
        # Hide new runners until they've been granted access at least once
        if not r.get("access_granted_at") and not r.get("armory_access"):
            continue
        xan = xanax_in_week(r, offset)
        has_access = r.get("armory_access", False)
        expiry = r.get("access_expires")
        expiry_str = "—"
        if expiry:
            try:
                expiry_str = datetime.fromisoformat(expiry).strftime("%b %d")
            except Exception:
                pass
        cr = weeks_remaining(r)

        if offset > 0:
            had_submissions = any(
                week_start.replace(tzinfo=__import__("datetime").timezone.utc)
                <= __import__("datetime").datetime.fromisoformat(s["submitted_at"]).replace(tzinfo=__import__("datetime").timezone.utc)
                < week_end.replace(tzinfo=__import__("datetime").timezone.utc)
                for s in r.get("submissions", [])
            )
            if not had_submissions:
                continue

        marker = "❌ " if not has_access else ""
        line = f"{marker}**{name}** — {cr}cr — {xan}/{WEEKLY_QUOTA} Xan — exp {expiry_str}"
        if has_access and cr != "0":
            active_lines.append(line)
        else:
            # For past weeks, always include if they had submissions that week
            # For current week, only show inactive if they had a submission within the last 7 days
            if offset > 0:
                inactive_lines.append(line)
            else:
                now = datetime.now(timezone.utc)
                cutoff = now - __import__("datetime").timedelta(days=7)
                recent_sub = any(
                    datetime.fromisoformat(s["submitted_at"]).replace(tzinfo=timezone.utc) >= cutoff
                    for s in r.get("submissions", [])
                )
                if recent_sub:
                    inactive_lines.append(line)

    if active_lines:
        embed.description += "**— Active —**\n" + "\n".join(active_lines)
    if inactive_lines:
        if active_lines:
            embed.description += "\n\n"
        embed.description += "**— Inactive —**\n" + "\n".join(inactive_lines)

    if pending_lines:
        if active_lines or inactive_lines:
            embed.description += "\n\n"
        embed.description += "**— Pending —**\n" + "\n".join(pending_lines)

    if not active_lines and not inactive_lines and not pending_lines:
        embed.description += "No submissions this week."

    total = len(active_lines) + len(inactive_lines) + len(pending_lines)
    embed.set_footer(text=f"{len(active_lines)} active / {len(inactive_lines)} inactive / {len(pending_lines)} pending — {total} total runners")
    return embed


def build_leader_embed_pending(page: int = 0):
    pending = get_pending_submissions()
    page_size = 4
    total = len(pending)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = page * page_size
    page_items = pending[start:start + page_size]

    embed = discord.Embed(
        title=f"Pending Approval — Page {page + 1}/{total_pages} ({total} total)",
        colour=COL_GOLD
    )
    if not pending:
        embed.description = "No pending submissions."
    else:
        lines = []
        for discord_id, i, r, s in page_items:
            name = r.get("torn_name") or "Unknown"
            display = f"{name} [{r.get('torn_id', '?')}]"
            ts = int(datetime.fromisoformat(s["submitted_at"]).timestamp())
            flag = " 🚨 MISMATCH" if s.get("log_mismatch") else (" ⚠ UNVERIFIED" if s.get("self_reported") else "")
            if s.get("partial"):
                flag += " ⚠️ PARTIAL"
            # Add status tag based on current armory access
            if not r.get("armory_access"):
                status_tag = " · 🆕 new runner"
            elif r.get("access_expires"):
                try:
                    exp_dt = datetime.fromisoformat(r["access_expires"]).replace(tzinfo=timezone.utc)
                    status_tag = f" · expires {exp_dt.strftime('%m/%d')}"
                except Exception:
                    status_tag = ""
            else:
                status_tag = ""
            lines.append(f"**#{s['id']} — {display}** — {s['xanax_deposited']} Xanax — <t:{ts}:R>{flag}{status_tag}")
            if s.get("self_reported") or s.get("log_mismatch"):
                lines.append(f"↳ *{s.get('api_status', '—')}*")
        embed.description = "\n".join(lines)
    return embed


def build_review_embed(runner_discord_id: str, sub_index: int):
    data = load_data()
    runner = data["runners"].get(str(runner_discord_id))
    if not runner:
        return discord.Embed(title="Error", description="Runner not found.", colour=COL_RED)
    s = runner["submissions"][sub_index]
    name = runner.get("torn_name") or "Unknown"
    display = f"{name} [{runner.get('torn_id', '?')}]"
    ts = int(datetime.fromisoformat(s["submitted_at"]).timestamp())

    flags = ""
    if s.get("log_mismatch"):
        flags = "\n🚨 **MISMATCH — self-reported amount differs from API**"
    elif s.get("self_reported"):
        flags = "\n⚠ **UNVERIFIED — API unavailable, amount is self-reported**"

    embed = discord.Embed(
        title=f"Review — {display}",
        colour=COL_RED if s.get("log_mismatch") else (0xFFA726 if s.get("self_reported") else COL_GOLD)
    )
    embed.add_field(name="Submission", value=f"#{s['id']} — <t:{ts}:F>", inline=True)
    embed.add_field(name="Xanax", value=str(s["xanax_deposited"]), inline=True)
    embed.add_field(name="API Status", value=s.get("api_status", "—") + flags, inline=False)
    embed.add_field(name="Deposit Log", value=f"```{s['deposit_log'][:400]}```", inline=False)
    embed.set_footer(text="Approve or Reject this submission")
    return embed


def build_leader_embed_reimburse():
    data = load_data()
    embed = discord.Embed(title="Pending Reimbursement", colour=COL_GOLD)
    lines, total = [], 0
    for r in data["runners"].values():
        for s in r["submissions"]:
            if s["status"] == "approved" and not s.get("reimbursed"):
                name = r.get("torn_name") or str(r["discord_id"])
                torn_id = r.get("torn_id", "")
                owed = s.get("credits_earned", 0) * REIMBURSEMENT
                total += owed
                reimburse_url = f"https://www.torn.com/factions.php?step=your#/tab=controls&addMoneyTo={torn_id}&money={int(owed)}"
                lines.append(f"**{name}** — {s.get('credits_earned', 0)} credit(s) — {owed:,.0f} — [Send Payment]({reimburse_url})")
    embed.description = "\n".join(lines) if lines else "All runners reimbursed."
    if lines:
        embed.set_footer(text=f"Total owed: {total:,.0f}")
    return embed


def count_pending_access():
    return len(get_pending_access_runners())


def get_pending_reimbursements():
    data = load_data()
    pending = []
    for discord_id, r in data["runners"].items():
        for i, s in enumerate(r["submissions"]):
            if s["status"] == "approved" and not s.get("reimbursed"):
                pending.append((discord_id, i, r, s))
    return pending


def get_pending_access_runners():
    """Runners with an approved submission that hasn't been turned into access yet."""
    data = load_data()
    result = []
    for discord_id, r in data["runners"].items():
        if r.get("armory_access"):
            continue
        if r.get("access_dismissed"):
            continue    
        approved = [s for s in r.get("submissions", []) if s.get("status") == "approved"]
        if not approved:
            continue
        latest_approval = max(
            (s.get("approved_at") for s in approved if s.get("approved_at")),
            default=None
        )
        if not latest_approval:
            continue
        if r.get("access_granted_at") and r.get("access_expires") is None:
            if latest_approval <= r.get("access_granted_at"):
                continue
        granted_at = r.get("access_granted_at")
        if granted_at and latest_approval <= granted_at:
            continue
        result.append((discord_id, r))
    return result

def build_leader_embed_access_pending():
    runners = get_pending_access_runners()
    embed = discord.Embed(title="Pending Access", colour=COL_GOLD)
    if not runners:
        embed.description = "All approved runners have armory access."
        return embed
    lines = []
    for discord_id, r in runners:
        name = r.get("torn_name") or "Unknown"
        tid = r.get("torn_id", "?")
        scheduled = r.get("access_scheduled_for")
        if scheduled:
            try:
                ts = int(datetime.fromisoformat(scheduled).timestamp())
                status = f"⏰ Scheduled <t:{ts}:R>"
            except Exception:
                status = "⏰ Scheduled"
        else:
            status = "⚠️ Not scheduled"
        lines.append(f"**{name} [{tid}]** — {status}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{len(runners)} runner(s) awaiting access")
    return embed


def build_leader_embed_access():
    data = load_data()
    ARMORY_URL = "https://www.torn.com/factions.php?step=your&type=1#/tab=controls&option=members"
    embed = discord.Embed(title="Armory Access", description=f"[Open Faction Armory]({ARMORY_URL})", colour=COL_GOLD)
    granted, revoked = [], []
    for r in data["runners"].values():
        if r.get("is_dealer") and not r.get("is_runner"):
            continue
        name = r.get("torn_name") or str(r["discord_id"])
        expiry = r.get("access_expires")
        exp_str = f" (expires <t:{int(datetime.fromisoformat(expiry).timestamp())}:D>)" if expiry else ""
        if r.get("armory_access"):
            granted.append(f"{name}{exp_str}")
        else:
            revoked.append(name)
    embed.add_field(name="Access Granted", value="\n".join(granted) or "None", inline=True)
    embed.add_field(name="No Access", value="\n".join(revoked) or "None", inline=True)
    return embed


async def build_live_feed_embed(dealer_api_key=None):
    """Show 13+ Xanax deposits from persistent armory feed log."""
    from tasks import load_feed
    feed = load_feed()
    entries = [e for e in feed.get("entries", []) if e.get("quantity", 0) >= 13]

    embed = discord.Embed(title="Live Feed — Xanax Deposits (13+)", colour=COL_GOLD)

    if not entries:
        embed.description = "No full deposits recorded yet — feed updates every 40 seconds."
        return embed

    lines = []
    for e in entries[:20]:
        lines.append(f"<t:{e['timestamp']}:R> — **{e['name']}** deposited **{e['quantity']}x** Xanax")

    embed.description = "\n".join(lines)
    full_count = sum(1 for e in entries if e.get("quantity", 0) >= 94)
    partial_count = sum(1 for e in entries if e.get("quantity", 0) < 94)
    footer = f"{full_count} full"
    if partial_count:
        footer += f" / {partial_count} partial"
    footer += " — updates every 60s"
    embed.set_footer(text=footer)
    return embed


async def build_dev_embed(dealer_api_key):
    """Raw verbose armory news for debugging — TEST_IDS only."""
    import re
    from api import fetch_armorynews
    logs, err = await fetch_armorynews(dealer_api_key)

    embed = discord.Embed(title="Dev — Raw Armory Feed", colour=0x888888)

    if err:
        embed.description = f"API error: {err}"
        return embed

    xanax_logs = []
    for log_id, log in logs.items():
        if not isinstance(log, dict):
            continue
        news = log.get("news", "")
        match = re.search(r'">(.+?)</a> deposited (\d+)x Xanax', news)
        if match and int(match.group(2)) >= 5:
            xanax_logs.append((log_id, log, match.group(1), int(match.group(2))))

    xanax_logs.sort(key=lambda x: x[1].get("timestamp", 0), reverse=True)

    embed.add_field(name="Total news entries", value=str(len(logs)), inline=True)
    embed.add_field(name="Xanax entries (5+)", value=str(len(xanax_logs)), inline=True)
    embed.add_field(name="Regex pattern", value="`deposited Nx Xanax`", inline=True)

    if xanax_logs:
        raw_lines = [
            f"id={lid} | {name} | qty={qty} | ts={log.get('timestamp')}"
            for lid, log, name, qty in xanax_logs[:10]
        ]
        embed.add_field(name="Matched entries (latest 10)", value="```\n" + "\n".join(raw_lines) + "\n```", inline=False)
    else:
        embed.description = "No Xanax deposits of 5+ found."

    sample = [(lid, log) for lid, log in list(logs.items())[:3] if isinstance(log, dict)]
    if sample:
        val = "\n".join(f"id={lid}: {log}" for lid, log in sample)[:900]
        embed.add_field(name="Sample raw entries", value=f"```\n{val}\n```", inline=False)

    embed.set_footer(text="Dev view — TEST_IDS only")
    return embed


# ── NEW v2.6 EMBEDS ────────────────────────────────────────────────────────

def build_calendar_embed(scheduled_dt: datetime, dealer_name: str) -> discord.Embed:
    """
    Ephemeral confirmation embed showing a text calendar with the chosen
    date marked, plus Discord timestamps for the scheduled time.
    """
    year = scheduled_dt.year
    month = scheduled_dt.month
    day = scheduled_dt.day
    month_name = scheduled_dt.strftime("%B %Y")

    cal = _calendar.monthcalendar(year, month)
    lines = [f"    {month_name}", "Mo Tu We Th Fr Sa Su"]
    for week in cal:
        row = []
        for d in week:
            if d == 0:
                row.append("  ")
            elif d == day:
                row.append(f"[{d:02d}]"[:2] + "!")  # mark chosen day
            else:
                row.append(f"{d:2}")
        lines.append(" ".join(row))

    # Rebuild cleanly — mark chosen day with > prefix
    lines2 = [f"    {month_name}", "Mo Tu We Th Fr Sa Su"]
    for week in cal:
        row = []
        for d in week:
            if d == 0:
                row.append("  ")
            elif d == day:
                row.append(f">{d:02d}")
            else:
                row.append(f" {d:02d}" if d < 10 else f"{d:2}")
        lines2.append("  ".join(row))

    embed = discord.Embed(
        title="📅 Confirm Access Schedule",
        description="```\n" + "\n".join(lines2) + "\n```",
        colour=COL_GOLD
    )
    embed.add_field(
        name="Scheduled For",
        value=f"**{scheduled_dt.strftime('%d %b %Y at %H:%M')} UTC (Torn Time)**",
        inline=False
    )
    embed.add_field(
        name="Countdown",
        value=f"<t:{int(scheduled_dt.timestamp())}:F> — <t:{int(scheduled_dt.timestamp())}:R>",
        inline=False
    )
    embed.set_footer(text=f"Set by {dealer_name} — confirm or edit below")
    return embed


def build_approved_submission_embed(runner_discord_id: str, sub_index: int) -> discord.Embed:
    """
    Persistent embed shown on an approved submission.
    Reflects current access schedule / grant status.
    Hosts the Set Access Time and Revert Approval buttons.
    """
    data = load_data()
    runner = data["runners"].get(str(runner_discord_id))
    if not runner:
        return discord.Embed(title="Error", description="Runner not found.", colour=COL_RED)

    sub = runner["submissions"][sub_index]
    name = runner.get("torn_name") or "Unknown"
    tid = runner.get("torn_id", "?")
    display = f"{name} [{tid}]"

    torn_id = runner.get("torn_id", "")
    credits_earned = sub.get("credits_earned", 0)
    owed = credits_earned * REIMBURSEMENT
    reimburse_url = (
        f"https://www.torn.com/factions.php?step=your#/tab=controls"
        f"&addMoneyTo={torn_id}&money={int(owed)}"
    )

    scheduled_for = runner.get("access_scheduled_for")
    access_granted = runner.get("armory_access", False)
    access_granted_at = runner.get("access_granted_at")
    access_expires = runner.get("access_expires")

    if access_granted and access_granted_at:
        try:
            ts = int(datetime.fromisoformat(access_granted_at).timestamp())
            access_line = f"✅ **Granted** — <t:{ts}:F>"
        except Exception:
            access_line = "✅ **Granted**"
        if access_expires:
            try:
                exp_ts = int(datetime.fromisoformat(access_expires).timestamp())
                access_line += f"\nExpires <t:{exp_ts}:D>"
            except Exception:
                pass
    elif scheduled_for:
        try:
            ts = int(datetime.fromisoformat(scheduled_for).timestamp())
            access_line = f"⏰ **Scheduled** — <t:{ts}:F> (<t:{ts}:R>)"
        except Exception:
            access_line = f"⏰ **Scheduled** — {scheduled_for}"
    else:
        access_line = "⚠️ **Not yet scheduled** — use Set Access Time below"

    title = f"Approved (Partial) — {display}" if credits_earned == 0 else f"Approved — {display}"
    colour = 0xFFA726 if credits_earned == 0 else COL_GREEN

    embed = discord.Embed(title=title, colour=colour)
    embed.add_field(name="Xanax", value=str(sub.get("xanax_deposited", "?")), inline=True)
    embed.add_field(name="Credits", value=str(credits_earned), inline=True)
    embed.add_field(
        name="Reimbursement",
        value=(
            f"[💰 Send ${owed:,.0f}]({reimburse_url})"
            if not sub.get("reimbursed") and owed > 0
            else ("✅ Sent" if sub.get("reimbursed") else "—")
        ),
        inline=True
    )
    embed.add_field(name="Armory Access", value=access_line, inline=False)

    approved_by = sub.get("approved_by", "?")
    approved_at = sub.get("approved_at")
    footer = f"Approved by {approved_by}"
    if approved_at:
        try:
            footer += f" — <t:{int(datetime.fromisoformat(approved_at).timestamp())}:R>"
        except Exception:
            pass

    reverted_at = sub.get("reverted_at")
    if reverted_at:
        footer += f" | Reverted by {sub.get('reverted_by', '?')}"

    embed.set_footer(text=footer)
    return embed



# ── FACTION ROLE CHECK ─────────────────────────────────────────────────────
# Reads only stored data (runner["role_check"]["position"], written by
# tasks.check_faction_roles) — no API calls, so it's instant.
def _ago(iso):
    try:
        return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:R>"
    except Exception:
        return "unknown"


def classify_roles():
    """Group runners by stored Torn position vs bot access.
    Returns {"mismatched": [(code, r, position)], "tap": [r], "sap": [r],
             "other": [(r, position, has)], "unknown": [(r, reason)]}"""
    data = load_data()
    acc_pos, no_pos = ACCESS_POSITION.lower(), NO_ACCESS_POSITION.lower()
    out = {"mismatched": [], "tap": [], "sap": [], "other": [], "unknown": []}
    for r in data["runners"].values():
        if r.get("is_dealer") and not r.get("is_runner"):
            continue
        rc = r.get("role_check") or {}
        if not r.get("torn_id"):
            out["unknown"].append((r, "no Torn ID"))
            continue
        if "position" not in rc:
            out["unknown"].append((r, "not checked yet"))
            continue
        position = rc["position"]
        if position is None:
            continue
        has = bool(r.get("armory_access"))
        pos = position.strip().lower()
        if pos == acc_pos:
            if has:
                out["tap"].append(r)
            else:
                out["mismatched"].append(("demote", r, position))
        elif pos == no_pos:
            if has:
                out["mismatched"].append(("promote", r, position))
            else:
                out["sap"].append(r)
        else:
            out["other"].append((r, position, has))
    return out


def build_role_check_embed():
    g = classify_roles()
    last = load_settings().get("last_role_check")
    nm = lambda r: r.get("torn_name") or f"[{r.get('discord_id')}]"
    embed = discord.Embed(title="Faction Role Check", colour=COL_RED if g["mismatched"] else COL_GREEN)
    parts = [f"Last API check {_ago(last)}" if last else "Not checked yet — runs every 15 min."]

    if g["mismatched"]:
        lines = []
        for code, r, position in g["mismatched"]:
            disp = f"{nm(r)} [{r.get('torn_id')}]"
            if code == "promote":
                lines.append(f"🔼 **{disp}** — has access, is {position}")
            else:
                lines.append(f"🔽 **{disp}** — expired, still {position}")
        parts.append("**⚠️ Needs Action**\n" + "\n".join(lines))
    else:
        parts.append(f"**⚠️ Needs Action**\nNone — all {ACCESS_POSITION}/{NO_ACCESS_POSITION} roles match.")

    correct = []
    if g["tap"]:
        correct.append(f"{ACCESS_POSITION} w/ access: " + ", ".join(nm(r) for r in g["tap"]))
    if g["sap"]:
        correct.append(f"{NO_ACCESS_POSITION} w/o access: " + ", ".join(nm(r) for r in g["sap"]))
    if g["other"]:
        correct.append("Other positions: " + ", ".join(
            f"{nm(r)} ({position}) — access {'✅' if has else '❌'}" for r, position, has in g["other"]))
    if correct:
        parts.append("**✅ Correct**\n" + "\n".join(correct))

    if g["unknown"]:
        parts.append("**❔ Not Checked**\n" + ", ".join(f"{nm(r)} ({reason})" for r, reason in g["unknown"]))

    desc = "\n\n".join(parts)
    embed.description = desc if len(desc) <= 4000 else desc[:3990] + "\n…"
    embed.set_footer(text=f"Mismatches ping @DrugDealer after the grace period")
    return embed
