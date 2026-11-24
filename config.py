# config.py — v2.8
# Changelog:
#   2.8 — Pending Access button, auto-extend on re-approval, ScheduleConfirmView fix
#   2.6 — Scheduled access time modal on approve, calendar confirmation, revert approval button
#   2.5 — Scheduled drug access (Grant Now vs 📅 Schedule), access expiry starts from grant not approval
#   2.4 — Dealers can opt-in as runners via Toggle Runner Status button
#   2.3 — Start Thread button on XRHelp, reimburse link in Pending Reimbursement
#   2.2 — Help button on runner dashboard, dealer Close Thread button
#   2.1 — Runner dashboard new fields: owed, deadline, streak, lifetime earnings, access banked
#   2.0 — New commands XRSubmit/XRManage/XRHelp, permanent runner threads
#   1.5 — Thread cleanup, 6-day submission alerts, !runner guide command
#   1.4 — AllRunnersView dropdown select menu for credit management
#   1.3 — Manual credit adjustment from All Runners view
#   1.2 — Feed-based verification, XID in feed entries, DEV_API_KEY, POLL_API_KEY
#   1.1 — Updated CHANNEL_ID, DRUG_DEALER_ROLE_ID for faction server
#   1.0 — Initial release
import os

VERSION = "lite-0.1"
TOKEN = os.getenv("TOKEN", "YOUR_BOT_TOKEN")
# Server, channel and role IDs come from Railway variables — no defaults, so
# runner-lite refuses to start rather than fall back to the live RSL server.
def _required_id(name):
    value = os.getenv(name, "").strip()
    if not value.isdigit():
        raise SystemExit(f"[CONFIG] {name} must be set to a Discord ID in Railway variables")
    return int(value)

GUILD_ID = _required_id("GUILD_ID")
CHANNEL_ID = _required_id("CHANNEL_ID")
DRUG_DEALER_ROLE_ID = _required_id("DRUG_DEALER_ROLE_ID")
MEMBER_ROLE_ID = _required_id("MEMBER_ROLE_ID")
# Owner Discord IDs (comma separated): leader rights everywhere, and !XRImport
TEST_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()]
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(DATA_DIR, "runner_data.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "runner_settings.json")
KEY_FILE = os.path.join(DATA_DIR, "runner.key")
WEEKLY_QUOTA = 94
REIMBURSEMENT = 66_000_000
# Faction role check (Torn positions, matched case-insensitively)
ACCESS_POSITION = "TapRoot"      # position that has armory Xanax access
NO_ACCESS_POSITION = "Sapling"   # position without access
ROLE_CHECK_GRACE_MIN = 840        # minutes a mismatch must persist before alerting
COL_GOLD = 0xC9A84C
COL_GREEN = 0x4CAF50
COL_RED = 0xE53935
COL_DARK = 0x2B2D31
