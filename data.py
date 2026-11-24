# data.py — v3.0
# Changelog:
#   3.0 — Credits refactor: access_expires is single source of truth.
#         Added weeks_remaining() derived display. Credits field vestigial.
#   2.8 — Added extend_access_expiry() shared helper to unify expiry math
#         across approval, manual credit adjust, and (future) auto-renew paths.
#   2.7 — Added migrate_pending_access(): one-time backfill of access_granted_at
#         so legacy/already-handled runners stop reappearing in "Pending Access".
#   2.6 — Added get_dealer_last_grant_time / set_dealer_last_grant_time
import json
import os
import hashlib
import math
from datetime import datetime, timezone, timedelta
from cryptography.fernet import Fernet
from config import DATA_FILE, SETTINGS_FILE, KEY_FILE, WEEKLY_QUOTA

# ── BANKED-WEEKS CAP POLICY ─────────────────────────────────────────────────
SOFT_CAP_WEEKS = 4   # crossing this triggers the submission lock
HARD_CAP_WEEKS = 6   # absolute ceiling — always blocks, no exceptions


# ── ENCRYPTION ─────────────────────────────────────────────────────────────
def get_fernet():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)
    return Fernet(key)


def encrypt(value: str) -> str:
    return get_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return get_fernet().decrypt(value.encode()).decode()


def hash_log(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


# ── PERSISTENCE ────────────────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"runners": {}}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {}


def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ── RUNNER RECORD ──────────────────────────────────────────────────────────
def get_runner(data, discord_id):
    key = str(discord_id)
    if key not in data["runners"]:
        data["runners"][key] = {
            "discord_id": discord_id,
            "torn_id": None,
            "torn_name": None,
            "torn_api_key_enc": None,
            "submissions": [],
            "credits": 0,
            "armory_access": False,
            "access_expires": None,
            "total_xanax": 0,
            "expiry_alerts": False,
            "weeks_banked_locked": False
        }
    return data["runners"][key]


# ── WEEK HELPERS ───────────────────────────────────────────────────────────
def get_week_start(offset=0):
    now = datetime.now(timezone.utc) - timedelta(weeks=offset)
    days_since_sun = (now.weekday() - 6) % 7
    return (now - timedelta(days=days_since_sun)).replace(hour=0, minute=0, second=0, microsecond=0)


def xanax_in_week(runner, offset=0):
    week_start = get_week_start(offset)
    week_end = week_start + timedelta(weeks=1)
    return sum(
        s["xanax_deposited"] for s in runner["submissions"]
        if s["status"] == "approved"
        and week_start <= datetime.fromisoformat(s["submitted_at"]).replace(tzinfo=timezone.utc) < week_end
    )


def fmt_week(offset=0):
    ws = get_week_start(offset)
    we = ws + timedelta(days=6)
    return f"{ws.strftime('%b %d')} – {we.strftime('%b %d, %Y')}"


# ── ACCESS EXPIRY HELPER ───────────────────────────────────────────────────
# BAND-AID — unifies the three slightly-different expiry-extension code paths
# (approval, manual credit adjust, auto-renew) into one rule, AND re-anchors
# any historical day-of-week drift (e.g. Saturday-anchored runners) to Sunday
# on their next addition.
#
# TODO REFACTOR: replace stored `access_expires` with a derived value computed
# from `credits` + a single Sunday anchor. Then this helper goes away entirely.
def extend_access_expiry(runner, weeks_to_add):
    """Extend (or set) a runner's access_expires by N weeks.

    Rules:
    - If access_expires exists and is in the future: add 7×weeks to it.
    - Otherwise: anchor to the next Sunday 00:00 UTC, then add 7×weeks.
    Returns the new expiry datetime.
    """
    now = datetime.now(timezone.utc)
    curr = runner.get("access_expires")
    base = None
    if curr:
        try:
            parsed = datetime.fromisoformat(curr).replace(tzinfo=timezone.utc)
            if parsed > now:
                base = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
        except Exception:
            base = None

    if base is None:
        # Sunday = weekday 6. (6 - now.weekday()) % 7 gives days until next Sunday.
        days_until_sun = (6 - now.weekday()) % 7
        if days_until_sun == 0 and (now.hour > 0 or now.minute > 0):
            days_until_sun = 7
        base = (now + timedelta(days=days_until_sun)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    new_expiry = base + timedelta(days=7 * weeks_to_add)
    runner["access_expires"] = new_expiry.isoformat()
    return new_expiry

def weeks_remaining(runner):
    """Derive display credits from access_expires. 1 week = 1 credit."""
    if not runner.get("armory_access") or not runner.get("access_expires"):
        return 0
    try:
        exp = datetime.fromisoformat(runner["access_expires"]).replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    if exp <= now:
        return 0
    return math.ceil((exp - now).total_seconds() / (7 * 86400))

def banked_weeks(runner):
    """True banked weeks from access_expires, ignoring armory_access entirely.
    Unlike weeks_remaining() (display-only, returns 0 when access is off),
    this reflects real accumulated credit regardless of whether the runner's
    access is currently toggled on — used anywhere the cap policy checks
    balance, so a dismissed/pending runner can't bank past the cap unseen.
    """
    if not runner.get("access_expires"):
        return 0
    try:
        exp = datetime.fromisoformat(runner["access_expires"]).replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    if exp <= now:
        return 0
    return math.ceil((exp - now).total_seconds() / (7 * 86400))
def grant_weeks(runner, sub, start_dt):
    """Weeks of access to give when a grant/schedule sets expiry from a start time.

    Never less than this submission's own credits (the old behaviour). If the
    runner's approvals have already stacked more whole weeks onto access_expires
    (e.g. 3 deposits approved before access was scheduled), returns that larger
    number so scheduling can't wipe banked weeks. Any doubt -> old behaviour.
    Call it BEFORE changing access_scheduled_for.
    """
    credits = sub.get("credits_earned", 1)
    try:
        exp = runner.get("access_expires")
        if not exp:
            return credits
        exp = datetime.fromisoformat(exp).replace(tzinfo=timezone.utc)
        prev = runner.get("access_scheduled_for")
        if prev:
            # Re-scheduling: the weeks were counted from the previously scheduled start
            anchor = datetime.fromisoformat(prev).replace(tzinfo=timezone.utc)
        else:
            # First grant: approvals stack from the Sunday after the latest approval
            approved = [s.get("approved_at") for s in runner.get("submissions", [])
                        if s.get("status") == "approved" and s.get("approved_at")]
            if not approved:
                return credits
            t = datetime.fromisoformat(max(approved)).replace(tzinfo=timezone.utc)
            days = (6 - t.weekday()) % 7
            if days == 0 and (t.hour > 0 or t.minute > 0):
                days = 7
            anchor = (t + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        if abs(start_dt - anchor) > timedelta(days=7):
            return credits      # start is nowhere near the expected one — don't guess
        banked = (exp - anchor + timedelta(days=1)) // timedelta(days=7)
        return max(credits, banked)
    except Exception:
        return credits
# ── BANKED-WEEKS CAP HELPERS ────────────────────────────────────────────────
def refresh_submission_lock(runner):
    """Clear the soft-cap lock once real banked weeks have decayed back under
    SOFT_CAP_WEEKS. Only clears — the lock is SET separately, at approval
    time, when a deposit pushes the balance over the soft cap. Returns True
    if this call just cleared the lock.
    """
    if runner.get("weeks_banked_locked") and banked_weeks(runner) < SOFT_CAP_WEEKS:
        runner["weeks_banked_locked"] = False
        return True
    return False


def can_submit_run(runner):
    """Return (allowed, reason) for whether this runner may submit a new run,
    per the banked-weeks cap policy. reason is None, 'hard_cap', or
    'soft_cap_locked'. Always refreshes the soft-cap lock first.
    """
    refresh_submission_lock(runner)
    if banked_weeks(runner) >= HARD_CAP_WEEKS:
        return False, "hard_cap"
    if runner.get("weeks_banked_locked"):
        return False, "soft_cap_locked"
    return True, None

# ── DEALER LAST GRANT TIME ─────────────────────────────────────────────────
def get_dealer_last_grant_time(discord_id: int) -> str | None:
    """Return the last grant time string a dealer used, e.g. '10/05/26 18:00'."""
    data = load_data()
    runner = data["runners"].get(str(discord_id), {})
    return runner.get("last_grant_time")


def set_dealer_last_grant_time(discord_id: int, time_str: str):
    """Persist the last grant time string for a dealer."""
    data = load_data()
    runner = get_runner(data, discord_id)
    runner["last_grant_time"] = time_str
    save_data(data)


# ── PENDING-ACCESS MIGRATION ───────────────────────────────────────────────
# "Pending Access" is computed live in embeds.get_pending_access_runners():
# a runner is pending when they have no armory_access but DO have an approved
# submission newer than their last grant (access_granted_at).
#
# Legacy / edge-case records can have an approved submission but a MISSING
# access_granted_at — e.g. they were granted before that field existed, or
# their submission was hand-edited. Those runners get stuck in "Pending Access"
# forever even though leadership already dealt with them.
#
# This one-time, idempotent migration stamps access_granted_at = their latest
# approved_at for any such runner, marking the existing approval as "already
# handled as of now". It is guarded by a settings flag so it runs exactly once.
#
# IMPORTANT — resubmission still works after this: a NEW submission approved
# later gets a fresh approved_at that is *newer* than access_granted_at, so the
# runner correctly reappears in "Pending Access". The migration only neutralises
# old approvals that predate the stamp; it never blocks future ones.


def migrate_pending_access():
    """One-time migration: clear legacy 'Pending Access' ghost records."""
    settings = load_settings()

    # Check if v281 migration already ran
    if settings.get('migrated_pending_access_v281'):
        return

    data = load_data()
    runners = data["runners"]
    fixed_count = 0

    for user_id, runner in runners.items():
        approved_at = runner.get('approved_at')
        access_granted_at = runner.get('access_granted_at')
        has_access = runner.get('armory_access', False)

        # Fix if: has approval + no access + (missing grant OR stale grant)
        if approved_at and not has_access:
            if not access_granted_at or approved_at > access_granted_at:
                runner['access_granted_at'] = approved_at
                fixed_count += 1

    if fixed_count > 0:
        save_data(data)
        print(f"✅ Migration v281: Fixed {fixed_count} stuck 'Pending Access' records", flush=True)

    # Mark migration as complete
    settings['migrated_pending_access_v281'] = True
    save_settings(settings)
# ── ACCESS HISTORY ─────────────────────────────────────────────────────────
def note_access(runner, by, action, detail=""):
    """Record one change to a runner's access: who did it, what, and the result.
    Newest last; keeps the latest 50. Never raises."""
    try:
        h = runner.setdefault("access_history", [])
        h.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "by": by,
            "action": action,
            "detail": (detail or "")[:200],
            "access": bool(runner.get("armory_access")),
            "expires": runner.get("access_expires"),
        })
        del h[:-50]
    except Exception:
        pass
