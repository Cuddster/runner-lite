# api.py — v1.2
# Changelog:
#   1.2 — verify_deposit now reads from armory_feed.json (less API calls, longer history)
#   1.1 — switched to v1 armorynews endpoint with regex parsing
#   1.0 — Initial release

import aiohttp
async def api_get(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()


# ── KEY VERIFICATION ───────────────────────────────────────────────────────
async def verify_torn_key(api_key):
    """Verify a basic runner key — just needs to identify the player."""
    data = await api_get(f"https://api.torn.com/v2/user/basic?key={api_key}")
    if "error" in data:
        return None, None, data["error"]["error"]
    profile = data.get("profile", {})
    return profile.get("id"), profile.get("name"), None


async def verify_dealer_key(api_key):
    """Verify a dealer key — must be Full/Limited/Custom AND have faction API access enabled."""
    user_data = await api_get(f"https://api.torn.com/v2/user/basic?key={api_key}")
    if "error" in user_data:
        return None, None, f"Invalid key: {user_data['error']['error']}"
    profile = user_data.get("profile", {})
    torn_id = profile.get("id")
    torn_name = profile.get("name")

    key_info = await api_get(f"https://api.torn.com/v2/key/info?key={api_key}")
    if "error" in key_info:
        return None, None, f"Could not read key info: {key_info['error']['error']}"

    access = key_info.get("info", {}).get("access", {})
    key_type = access.get("type", "")
    has_faction = access.get("faction", False)

    print(f"[DEALER KEY] {torn_name} [{torn_id}] | type={key_type} | faction={has_faction}")

    if key_type not in ("Limited Access", "Custom"):
        return None, None, (
            f"Wrong key type ({key_type}). You need a **Limited** or **Custom** key with Faction access enabled — not Full Access. "
            "Full keys expose too much. In Torn go to Preferences → API and create a Limited or Custom key."
        )
    if not has_faction:
        return None, None, (
            "Faction API access is not enabled on this key. "
            "In Torn go to Preferences → API → edit your key → enable Faction access."
        )

    return torn_id, torn_name, None


# ── ARMORY NEWS ────────────────────────────────────────────────────────────
async def fetch_armorynews(dealer_api_key):
    """Fetch raw faction armory news via v1 API."""
    data = await api_get(f"https://api.torn.com/faction/?selections=armorynews&key={dealer_api_key}")
    if "error" in data:
        return None, f"API error: {data['error']['error']}"
    return data.get("armorynews", {}), None


async def verify_deposit(torn_id, week_start_ts, dealer_api_key=None):
    """Check armory_feed.json for Xanax deposits by this player this week.
    Falls back to live API if feed has no entries for this player. v1.2"""
    import re
    import os
    import json

    data_dir = "/data" if os.path.isdir("/data") else "."
    feed_file = os.path.join(data_dir, "armory_feed.json")

    xanax_deposited = 0
    feed_checked = False

    if os.path.exists(feed_file):
        with open(feed_file) as f:
            feed = json.load(f)
        for entry in feed.get("entries", []):
            if entry.get("torn_id") != int(torn_id):
                continue
            if entry.get("timestamp", 0) < week_start_ts:
                continue
            feed_checked = True
            xanax_deposited += entry.get("quantity", 0)

    if feed_checked:
        print(f"[VERIFY] Found {xanax_deposited}x Xanax for torn_id={torn_id} in feed", flush=True)
        return xanax_deposited, None

    # Feed had no entries for this player — fall back to live API
    if not dealer_api_key:
        return None, "No entries in feed and no dealer key for fallback"

    print(f"[VERIFY] No feed entries for torn_id={torn_id}, falling back to live API", flush=True)
    logs, err = await fetch_armorynews(dealer_api_key)
    if err:
        return None, err

    for log in logs.values():
        if not isinstance(log, dict):
            continue
        if log.get("timestamp", 0) < week_start_ts:
            continue
        news = log.get("news", "")
        if f"XID={torn_id}" not in news:
            continue
        match = re.search(r"deposited (\d+)x Xanax", news)
        if match:
            xanax_deposited += int(match.group(1))

    return xanax_deposited, None


# ── FACTION POSITIONS ──────────────────────────────────────────────────────
async def fetch_faction_positions(api_key):
    """Return ({torn_id: position}, None) on success, or (None, error)."""
    data = await api_get(f"https://api.torn.com/v2/faction/members?key={api_key}")
    if "error" in data:
        return None, data["error"].get("error", "unknown error")
    members = data.get("members", [])
    if isinstance(members, dict):  # tolerate v1-style {id: {...}} shape
        members = [dict(v, id=k) for k, v in members.items()]
    positions = {}
    for m in members:
        if isinstance(m, dict) and m.get("id") is not None:
            try:
                positions[int(m["id"])] = m.get("position") or ""
            except (TypeError, ValueError):
                continue
    return positions, None
