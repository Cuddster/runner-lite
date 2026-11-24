# tasks.py — v4.0
# Changelog:
#   4.0 — Credits refactor: check_expiry is revoke-only, no credit consumption.
#         DMS_ENABLED disabled pending stability verification.
#   3.0 — All get_member() replaced with fetch_member() for reliable DMs, check_expiry 30min
#   2.9 — Dealer DM when runner access expires (deactivate reminder)
#   2.5 — Scheduled drug access task (process_scheduled_access)
#   1.5 — Thread cleanup task, 6-day submission alert to dealers
#   1.2 — XID stored in feed entries, update_feed_from_logs uses XID regex
#   1.1 — Poll interval 1 min, POLL_API_KEY env var, archive_raw_logs added
#   1.0 — Initial release

import json
import os
import re
import discord
from discord.ext import tasks
from datetime import datetime, timezone, timedelta
from config import (GUILD_ID, DATA_DIR, CHANNEL_ID, DRUG_DEALER_ROLE_ID,
                    ACCESS_POSITION, NO_ACCESS_POSITION, ROLE_CHECK_GRACE_MIN)
from data import load_data, save_data, decrypt, load_settings, save_settings, note_access
from api import fetch_faction_positions

bot_ref = None  # set from RunnerBot.py after bot is created
DMS_ENABLED = True  # set to True when ready to enable DMs
FEED_FILE = os.path.join(DATA_DIR, "armory_feed.json")
LOGS_FILE = os.path.join(DATA_DIR, "faction_logs.json")
MAX_PARTIAL = 500  # rolling cap for deposits under 94
ACTION_LOG_FILE = os.path.join(DATA_DIR, "action_log.json")

def log_action(kind, name, detail):
    log = []
    if os.path.exists(ACTION_LOG_FILE):
        try:
            with open(ACTION_LOG_FILE) as f:
                log = json.load(f)
        except Exception:
            log = []
    log.append({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "name": name, "detail": detail})
    log = log[-500:]
    with open(ACTION_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)


ARMORY_URL = "https://www.torn.com/factions.php?step=your&type=1#/tab=controls&option=members"


# ── FEED HELPERS ───────────────────────────────────────────────────────────
def load_feed():
    if os.path.exists(FEED_FILE):
        with open(FEED_FILE) as f:
            return json.load(f)
    return {"entries": []}


def save_feed(feed):
    with open(FEED_FILE, "w") as f:
        json.dump(feed, f, indent=2)


def load_logs():
    if os.path.exists(LOGS_FILE):
        with open(LOGS_FILE) as f:
            return json.load(f)
    return {"entries": {}}


def save_logs(logs):
    with open(LOGS_FILE, "w") as f:
        json.dump(logs, f, indent=2)


def archive_raw_logs(raw_logs: dict):
    """Store all armory news entries to faction_logs.json, deduplicated by log ID."""
    logs = load_logs()
    existing_ids = set(logs["entries"].keys())
    added = 0
    for log_id, log in raw_logs.items():
        if not isinstance(log, dict):
            continue
        if log_id in existing_ids:
            continue
        logs["entries"][log_id] = {
            "news": log.get("news", ""),
            "timestamp": log.get("timestamp", 0),
            "archived_at": int(datetime.now(timezone.utc).timestamp())
        }
        added += 1
    if added:
        save_logs(logs)
    return added


def get_dealer_keys():
    """Return list of decrypted dealer API keys."""
    data = load_data()
    keys = []
    for r in data["runners"].values():
        if r.get("is_dealer") and r.get("torn_api_key_enc"):
            try:
                keys.append(decrypt(r["torn_api_key_enc"]))
            except Exception:
                continue
    return keys


# ── EXPIRY TASK ─────────────────────────────────────────────────────────────
@tasks.loop(minutes=30, reconnect=True)
async def check_expiry():
    if check_expiry.current_loop == 0:
        return
    data = load_data()
    now = datetime.now(timezone.utc)
    guild = bot_ref.get_guild(GUILD_ID)
    if not guild:
        return
    changed = False

    for key, runner in data["runners"].items():
        if not runner.get("armory_access") or not runner.get("access_expires"):
            continue

        expiry = datetime.fromisoformat(runner["access_expires"]).replace(tzinfo=timezone.utc)
        hours_left = (expiry - now).total_seconds() / 3600

        # 24h warning DM to runner (if alerts enabled)
        if DMS_ENABLED and 23 < hours_left <= 24 and runner.get("expiry_alerts", False) and not runner.get("expiry_warning_sent"):
            try:
                member = await guild.fetch_member(int(key))
                await member.send(
                    f"⏰ Your Resilience armory Xanax access expires <t:{int(expiry.timestamp())}:R>.\n"
                    f"Submit your next run before Saturday to keep access."
                )
            except Exception:
                pass
            runner["expiry_warning_sent"] = True
            changed = True
            log_action("warn", runner.get("torn_name", key), "24h expiry warning sent")

        # Access expired — revoke
        if now >= expiry:
            runner_name = runner.get("torn_name", f"[{key}]")
            runner["armory_access"] = False
            runner["expiry_warning_sent"] = False
            changed = True
            print(f"[CHECK EXPIRY] {runner_name} access revoked — expired", flush=True)
            log_action("revoke", runner_name, "access revoked — expired")
            note_access(runner, "bot", "access expired")

            # DM runner
            if DMS_ENABLED:
                try:
                    member = await guild.fetch_member(int(key))
                    await member.send(
                        f"🔒 Your armory drug access has expired.\n"
                        f"Submit your next XR run to regain access."
                    )
                except Exception:
                    pass

                # DM all dealers to deactivate in-game
                for r in data["runners"].values():
                    if not r.get("is_dealer"):
                        continue
                    try:
                        dealer = await guild.fetch_member(int(r["discord_id"]))
                        await dealer.send(
                            f"🔒 **{runner_name}'s** armory drug access has expired.\n"
                            f"Please deactivate their access in-game: {ARMORY_URL}"
                        )
                    except Exception:
                        pass

    if changed:
        save_data(data)


@check_expiry.before_loop
async def before_check_expiry():
    await bot_ref.wait_until_ready()


# ── ARMORY FEED TASK ────────────────────────────────────────────────────────
async def update_feed_from_logs(logs: dict):
    """Parse armorynews logs and append new Xanax deposits to the feed file."""
    feed = load_feed()
    existing_ids = {e["log_id"] for e in feed["entries"]}
    new_entries = []

    for log_id, log in logs.items():
        if not isinstance(log, dict):
            continue
        if log_id in existing_ids:
            continue
        news = log.get("news", "")
        match = re.search(r'XID=(\d+)">(.+?)</a> deposited (\d+)x Xanax', news)
        if not match:
            continue
        qty = int(match.group(3))
        if qty < 13:
            continue
        new_entries.append({
            "log_id": log_id,
            "torn_id": int(match.group(1)),
            "name": match.group(2),
            "quantity": qty,
            "timestamp": log.get("timestamp", 0),
            "permanent": qty >= 94
        })

    if not new_entries:
        return 0

    feed["entries"].extend(new_entries)
    permanent = [e for e in feed["entries"] if e.get("permanent")]
    partial = [e for e in feed["entries"] if not e.get("permanent")]
    partial.sort(key=lambda x: x["timestamp"], reverse=True)
    partial = partial[:MAX_PARTIAL]
    feed["entries"] = permanent + partial
    feed["entries"].sort(key=lambda x: x["timestamp"], reverse=True)
    save_feed(feed)
    return len(new_entries)


@tasks.loop(minutes=1, reconnect=True)
async def poll_armory_feed():
    if poll_armory_feed.current_loop == 0:
        return

    poll_key = os.getenv("POLL_API_KEY")
    if not poll_key:
        print("[POLL] POLL_API_KEY not set, skipping", flush=True)
        return

    import aiohttp
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.torn.com/faction/?selections=armorynews&key={poll_key}"
            ) as resp:
                data = await resp.json()
    except Exception as e:
        print(f"[POLL] Request failed: {e}", flush=True)
        return

    if "error" in data:
        print(f"[POLL] API error: {data['error']['error']}", flush=True)
        return

    logs = data.get("armorynews", {})
    archive_raw_logs(logs)
    await update_feed_from_logs(logs)


@poll_armory_feed.before_loop
async def before_poll_armory_feed():
    await bot_ref.wait_until_ready()


# ── 6-DAY SUBMISSION ALERT TASK ─────────────────────────────────────────────
@tasks.loop(hours=12, reconnect=True)
async def submission_alerts():
    if submission_alerts.current_loop == 0:
        return
    guild = bot_ref.get_guild(GUILD_ID)
    if not guild:
        return

    data = load_data()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=6)

    dealers = []
    for r in data["runners"].values():
        if not r.get("is_dealer"):
            continue
        try:
            member = await guild.fetch_member(int(r["discord_id"]))
            dealers.append(member)
        except Exception:
            continue

    if not dealers:
        return

    alerts = []
    for r in data["runners"].values():
        if r.get("is_dealer") and not r.get("is_runner"):
            continue
        name = r.get("torn_name") or "Unknown"
        tid = r.get("torn_id", "?")
        display = f"{name} [{tid}]"

        for s in r.get("submissions", []):
            try:
                submitted_at = datetime.fromisoformat(s["submitted_at"]).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if submitted_at > cutoff:
                continue

            if s["status"] == "pending":
                alerts.append(f"⚠️ **{display}** has a submission pending approval for 6+ days")
            elif s["status"] == "approved" and not s.get("reimbursed"):
                alerts.append(f"⚠️ **{display}** is awaiting reimbursement for 6+ days")
            elif s["status"] == "approved" and s.get("reimbursed") and not r.get("armory_access") and not r.get("access_granted_at"):
                alerts.append(f"⚠️ **{display}** hasn't had drug access granted yet (6+ days)")

    if not alerts:
        return

    msg = "**Submission Follow-up Alerts**\n\n" + "\n".join(alerts)
    for dealer in dealers:
        try:
            await dealer.send(msg)
        except Exception:
            pass


@submission_alerts.before_loop
async def before_submission_alerts():
    await bot_ref.wait_until_ready()


# ── SCHEDULED ACCESS TASK ───────────────────────────────────────────────────
@tasks.loop(minutes=5, reconnect=True)
async def process_scheduled_access():
    if process_scheduled_access.current_loop == 0:
        return
    data = load_data()
    now = datetime.now(timezone.utc)
    guild = bot_ref.get_guild(GUILD_ID)
    changed = False

    for key, runner in data["runners"].items():
        scheduled = runner.get("access_scheduled_for")
        if not scheduled:
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now < scheduled_dt:
            continue
        
        if runner.get("armory_access"):
            runner.pop("access_scheduled_for", None)
            changed = True
            continue
        
        runner["armory_access"] = True
        runner["access_granted_at"] = now.isoformat()
        runner.pop("access_scheduled_for", None)
        changed = True
        print(f"[SCHEDULED] Granted access to {runner.get('torn_name', key)}", flush=True)
        log_action("grant", runner.get("torn_name", key), f"access granted, expires {runner.get('access_expires') or '—'}")
        note_access(runner, "bot", "scheduled access started")
        # DM runner
        if guild:
            try:
                member = await guild.fetch_member(int(key))
                expiry = runner.get("access_expires")
                exp_str = f" Expires **{datetime.fromisoformat(expiry).strftime('%b %d, %Y')}**." if expiry else ""
                await member.send(
                    f"🎉 Your armory drug access is now active!{exp_str}\n"
                    f"You can now access Xanax from the faction armory."
                )
            except Exception:
                pass

        # DM dealers to enable in-game
        if guild:
            for r in data["runners"].values():
                if not r.get("is_dealer"):
                    continue
                try:
                    dealer = await guild.fetch_member(int(r["discord_id"]))
                    await dealer.send(
                        f"📅 Scheduled access granted to **{runner.get('torn_name', 'Unknown')}**. "
                        f"Remember to enable drug access in the faction armory: {ARMORY_URL}"
                    )
                except Exception:
                    pass

    if changed:
        save_data(data)


@process_scheduled_access.before_loop
async def before_process_scheduled_access():
    await bot_ref.wait_until_ready()

# ── CREDIT CONSUMPTION ───────────────────────────────────────────────────────
# NOTE: Credits are no longer consumed by check_expiry. access_expires is the
# single source of truth — set at approval time via extend_access_expiry().
# check_expiry simply revokes access when the expiry date passes.



# ── FACTION ROLE CHECK TASK ────────────────────────────────────────────────
# Compares each runner's Torn faction position with the bot's armory_access
# and pings @DrugDealer when they disagree for ROLE_CHECK_GRACE_MIN minutes.
#   access + Sapling       -> promote     | no access + TapRoot -> demote
#   access flips while in any other position -> one-time "verify privileges"
# Per-runner state lives in runner["role_check"]. Alerts once per mismatch;
# posts a resolved note when a flagged TapRoot/Sapling mismatch is fixed.
def _role_alert_text(code, display, position):
    return {
        "promote":       f"🔼 **{display}** has XR access but is **{position}**. Promote to {ACCESS_POSITION}.",
        "demote":        f"🔽 **{display}**'s XR access has expired but they're still **{position}**. Demote to {NO_ACCESS_POSITION}.",
        "verify_grant":  f"🔍 **{display}** was granted XR access and holds **{position}**. Verify their armory privileges.",
        "verify_revoke": f"🔍 **{display}**'s XR access has expired and they hold **{position}**. Verify their privileges were removed.",
    }.get(code, f"❓ **{display}** — role check: {code}")


@tasks.loop(minutes=15, reconnect=True)
async def check_faction_roles():
    # 1. Fetch positions FIRST, before loading data, so there's no await
    #    between load_data() and save_data() (avoids overwriting other writers).
    positions, err = None, "no dealer keys"
    for key in get_dealer_keys():
        positions, err = await fetch_faction_positions(key)
        if positions is not None:
            break
    if positions is None:
        print(f"[ROLE CHECK] skipped — {err}", flush=True)
        return

    # 2. Compare and update state (no awaits in this block).
    data = load_data()
    now = datetime.now(timezone.utc)
    grace = timedelta(minutes=ROLE_CHECK_GRACE_MIN)
    acc_pos, no_pos = ACCESS_POSITION.lower(), NO_ACCESS_POSITION.lower()
    alerts, resolved = [], []
    changed = False

    for key, r in data["runners"].items():
        if r.get("is_dealer") and not r.get("is_runner"):
            continue
        try:
            torn_id = int(r.get("torn_id"))
        except (TypeError, ValueError):
            continue
        if torn_id not in positions:
            # not in faction — record for dashboard, leave alert state alone
            rc = r.get("role_check") or {}
            if rc.get("position", "") is not None:
                rc["position"] = None
                r["role_check"] = rc
                changed = True
            continue

        position = positions[torn_id] or "no position"
        pos = position.strip().lower()
        has = bool(r.get("armory_access"))
        display = f"{r.get('torn_name') or 'Unknown'} [{torn_id}]"
        rc = r.get("role_check") or {}
        before = dict(rc)
        prev = rc.get("last_access")
        rc["last_access"] = has
        rc["position"] = position
        current = rc.get("issue")

        if pos == acc_pos:
            issue = None if has else "demote"
        elif pos == no_pos:
            issue = "promote" if has else None
        elif prev is not None and prev != has:
            issue = "verify_grant" if has else "verify_revoke"
        elif current and current.startswith("verify") and not rc.get("alerted"):
            issue = current  # still inside grace window
        else:
            issue = None

        if issue is None:
            if current and rc.get("alerted") and not current.startswith("verify"):
                resolved.append(f"✅ **{display}** is now **{position}** — resolved.")
            rc.update(issue=None, since=None, alerted=False)
        elif issue != current:
            rc.update(issue=issue, since=now.isoformat(), alerted=False)
        elif not rc.get("alerted"):
            try:
                since = datetime.fromisoformat(rc["since"]).replace(tzinfo=timezone.utc)
            except Exception:
                since = now
                rc["since"] = now.isoformat()
            if now - since >= grace:
                alerts.append(_role_alert_text(issue, display, position))
                rc["alerted"] = True
                log_action("role", r.get("torn_name", key), f"{issue} ({position})")

        if rc != before:
            r["role_check"] = rc
            changed = True

    if changed:
        save_data(data)
    settings = load_settings()
    settings["last_role_check"] = now.isoformat()
    save_settings(settings)
  
    # 3. Post (after save).
    lines = alerts + resolved
    if not lines:
        return
    guild = bot_ref.get_guild(GUILD_ID)
    if guild is None:
        print("[ROLE CHECK] guild not found — DMs not sent", flush=True)
        return
    if not guild.chunked:
        try:
            await guild.chunk()
        except Exception as e:
            print(f"[ROLE CHECK] member chunk failed: {e}", flush=True)
    role = guild.get_role(DRUG_DEALER_ROLE_ID)
    dealers = [m for m in (role.members if role else []) if not m.bot]
    if not dealers:
        print("[ROLE CHECK] no members with DrugDealer role — DMs not sent", flush=True)
        return
    chunks, buf = [], "**Faction Role Check**\n"
    for line in lines:
        if len(buf) + len(line) + 1 > 1900:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    chunks.append(buf)
    sent = 0
    for member in dealers:
        try:
            for chunk in chunks:
                await member.send(chunk)
            sent += 1
        except Exception as e:
            print(f"[ROLE CHECK] DM failed for {member.display_name} ({member.id}): {e}", flush=True)
    print(f"[ROLE CHECK] DM'd {sent}/{len(dealers)} DrugDealer(s): "
          f"{len(alerts)} alert(s), {len(resolved)} resolved", flush=True)


@check_faction_roles.before_loop
async def before_check_faction_roles():
    await bot_ref.wait_until_ready()
