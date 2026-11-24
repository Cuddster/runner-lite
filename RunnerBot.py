# RunnerBot.py — v2.8.1
# Changelog:
#   2.8 — Pending Access button on dashboard, PendingAccessView, auto-extend on re-approval, fixed ScheduleConfirmView
#   2.6 — GrantAccessModal on approve, calendar confirm, ApprovedSubmissionView, RevertConfirmView
#   2.1 — Internal data server on port 8081 for Armory dashboard
#   2.0 — New commands XRSubmit/XRManage/XRHelp, permanent runner threads
#   1.5 — Thread cleanup, 6-day submission alerts
#   1.4 — AllRunnersView dropdown
#   1.3 — Manual credit adjustment
#   1.2 — Feed-based verification
#   1.1 — Faction server migration
#   1.0 — Initial release

import sys
import os
import hmac
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import discord
from discord.ext import commands
from aiohttp import web as _web
from config import TOKEN, VERSION, GUILD_ID, CHANNEL_ID, DRUG_DEALER_ROLE_ID, MEMBER_ROLE_ID, TEST_IDS, COL_GOLD, DATA_DIR
from data import load_data, save_data, get_runner, migrate_pending_access
from embeds import build_leader_embed_dashboard, build_xr_embed
from views import (XRView, LeaderView, DealerLinkView, is_leader,
                   ReimburseView, DrugAccessView, DoneView, PendingView, ReviewView,
                   LiveFeedView, DevView, RefreshXRButton, ReimburseListView, AllRunnersView,
                   HelpThreadView, ApprovedSubmissionView, PendingAccessView, RoleCheckView)
import tasks as task_module

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
task_module.bot_ref = bot

INTERNAL_SECRET  = os.environ.get("INTERNAL_SECRET", "")
INTERNAL_PORT    = int(os.environ.get("INTERNAL_PORT", 8081))
# Public monitoring API: set MONITOR_API_KEY in Railway env vars to enable.
MONITOR_API_KEY  = os.environ.get("MONITOR_API_KEY", "")
# Railway injects PORT for the publicly-routed port. Fall back to INTERNAL_PORT
# for local runs. The web server binds to this so the monitor API is reachable
# over the public Railway domain.
PUBLIC_PORT      = int(os.environ.get("PORT", INTERNAL_PORT))


# ── HELPERS ────────────────────────────────────────────────────────────────
async def get_or_create_runner_thread(ctx, runner, data):
    thread_id = runner.get("thread_id")
    if thread_id:
        try:
            thread = await ctx.guild.fetch_channel(thread_id)
            if thread and not thread.archived:
                await ctx.send(f"<@{ctx.author.id}> You already have an active thread: {thread.mention}", delete_after=10)
                return thread, False
        except Exception:
            pass
    thread = await ctx.channel.create_thread(
        name=f"XR — {ctx.author.display_name}",
        type=discord.ChannelType.private_thread,
        invitable=False
    )
    await thread.add_user(ctx.author)
    runner["thread_id"] = thread.id
    save_data(data)
    return thread, True


# ── COMMANDS ───────────────────────────────────────────────────────────────
def channel_check(ctx):
    return ctx.channel.id == CHANNEL_ID or ctx.author.id in TEST_IDS


@bot.command(name="XRSubmit")
async def xr_submit(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    if not channel_check(ctx):
        return await ctx.send("This command can only be used in the designated runner channel.", delete_after=5)
    if isinstance(ctx.channel, discord.Thread):
        return await ctx.send("Please run this command in the main channel.", delete_after=5)
    member = ctx.guild.get_member(ctx.author.id)
    if not member:
        return
    if not any(r.id == MEMBER_ROLE_ID for r in member.roles) and ctx.author.id not in TEST_IDS:
        return await ctx.send("Verified faction members only.", delete_after=5)
    data = load_data()
    runner = get_runner(data, ctx.author.id)
    linked = runner.get("torn_id") is not None
    thread, is_new = await get_or_create_runner_thread(ctx, runner, data)
    if is_new:
        msg = await thread.send(embed=build_xr_embed(runner), view=XRView(ctx.author.id, linked))
        runner["thread_message_id"] = msg.id
        save_data(data)


@bot.command(name="XRManage")
async def xr_manage(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    if not channel_check(ctx):
        return await ctx.send("This command can only be used in the designated runner channel.", delete_after=5)
    if isinstance(ctx.channel, discord.Thread):
        return await ctx.send("Please run this command in the main channel.", delete_after=5)
    member = ctx.guild.get_member(ctx.author.id)
    if not member or not is_leader(member):
        return await ctx.send("DrugDealer role required.", delete_after=5)
    data = load_data()
    runner = get_runner(data, ctx.author.id)
    linked = runner.get("torn_api_key_enc") is not None and runner.get("is_dealer")
    thread = await ctx.channel.create_thread(
        name=f"Dealer — {ctx.author.display_name}",
        type=discord.ChannelType.private_thread,
        invitable=False
    )
    await thread.add_user(ctx.author)
    if linked:
        await thread.send(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard", 0))
    else:
        embed = discord.Embed(
            title="Link Your Torn Account",
            description=(
                "To use the dealer panel you need a Torn API key with **Faction API access** enabled.\n\n"
                "**How to set this up:**\n"
                "1. Go to [Torn Preferences → API](https://www.torn.com/preferences.php#tab=api)\n"
                "2. Create a new key or edit an existing one\n"
                "3. Set access level to **Limited** or **Custom**\n"
                "4. Enable **Faction** access\n"
                "5. Paste the key below"
            ),
            colour=COL_GOLD
        )
        await thread.send(embed=embed, view=DealerLinkView())


@bot.command(name="XRHelp")
async def xr_help(ctx):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    if not channel_check(ctx):
        return await ctx.send("This command can only be used in the designated runner channel.", delete_after=5)
    member = ctx.guild.get_member(ctx.author.id)
    if not member or not is_leader(member):
        return await ctx.send("DrugDealer role required.", delete_after=5)
    embed = discord.Embed(title="🧪 Xanax Runner Program", colour=COL_GOLD)
    embed.description = (
        "Welcome to the Resilience Xanax Runner program. As a runner you deposit Xanax into the "
        "faction armory weekly in exchange for reimbursement and armory drug access."
    )
    embed.add_field(name="Getting Started", value="1. Run `!XRSubmit` in this channel\n2. Link your Torn account when prompted\n3. That's it — your thread is ready", inline=False)
    embed.add_field(name="Weekly Requirements", value="• Deposit **94 Xanax** into the Resilience faction armory\n• Copy your deposit log\n• Submit before **Saturday**", inline=False)
    embed.add_field(name="What You Get", value="• **$66,000,000** reimbursement per full deposit\n• Armory drug access for the following week\n• Credits stack", inline=False)
    embed.add_field(name="Submitting a Run", value="1. Deposit 94 Xanax\n2. Open your thread with `!XRSubmit`\n3. Click **Submit Run**\n4. Enter amount and paste deposit log\n5. Wait for approval", inline=False)
    embed.add_field(name="Questions?", value="Contact a Resilience Leader or Elder.", inline=False)
    embed.set_footer(text=f"Resilience — Runner Program v{VERSION}")
    await ctx.send(embed=embed, view=StartThreadView())


# ── START THREAD VIEW ──────────────────────────────────────────────────────
class StartThreadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🧪 Start My Thread", style=discord.ButtonStyle.primary, custom_id="guide_start_thread")
    async def start_thread(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return await interaction.response.send_message("Could not verify membership.", ephemeral=True)
        if not any(r.id == MEMBER_ROLE_ID for r in member.roles) and interaction.user.id not in TEST_IDS:
            return await interaction.response.send_message("Verified faction members only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        runner = get_runner(data, interaction.user.id)
        linked = runner.get("torn_id") is not None
        thread_id = runner.get("thread_id")
        if thread_id:
            try:
                thread = await interaction.guild.fetch_channel(thread_id)
                if thread and not thread.archived:
                    return await interaction.followup.send(f"You already have an active thread: {thread.mention}", ephemeral=True)
            except Exception:
                pass
        channel = interaction.channel
        thread = await channel.create_thread(
            name=f"XR — {interaction.user.display_name}",
            type=discord.ChannelType.private_thread,
            invitable=False
        )
        await thread.add_user(interaction.user)
        msg = await thread.send(embed=build_xr_embed(runner), view=XRView(interaction.user.id, linked))
        runner["thread_id"] = thread.id
        runner["thread_message_id"] = msg.id
        save_data(data)
        await interaction.followup.send(f"Your thread is ready: {thread.mention}", ephemeral=True)


# ── LEGACY ─────────────────────────────────────────────────────────────────
@bot.command(name="runner")
async def runner_legacy(ctx, subcommand=None):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    await ctx.send(
        "⚠️ This command has been updated. Please use:\n"
        "• `!XRSubmit` — to open your runner thread\n"
        "• `!XRManage` — dealer dashboard\n"
        "• `!XRHelp` — guide",
        delete_after=86400
    )


# ── INTERNAL DATA SERVER ────────────────────────────────────────────────────
async def _internal_data_handler(request):
    secret = request.headers.get("X-Internal-Secret", "")
    if not INTERNAL_SECRET or secret != INTERNAL_SECRET:
        return _web.Response(status=403, text="Forbidden")
    import json as _json
    payload = {}
    for fname in ["faction_logs.json", "armory_feed.json", "runner_data.json", "action_log.json"]:
        path = os.path.join(DATA_DIR, fname)
        try:
            with open(path) as f:
                payload[fname] = _json.load(f)
        except Exception:
            payload[fname] = {}
    return _web.json_response(payload)


def _check_monitor_auth(request):
    """Validate the Authorization header against MONITOR_API_KEY.

    Accepts `Authorization: Bearer <key>` (preferred) or a bare `<key>`.
    Uses a constant-time comparison to avoid timing attacks. Returns an
    aiohttp Response on failure, or None when the request is authorized.
    """
    if not MONITOR_API_KEY:
        return _web.json_response(
            {"error": "Monitoring API disabled (MONITOR_API_KEY not set)."},
            status=503,
        )
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
    if not token or not hmac.compare_digest(token, MONITOR_API_KEY):
        return _web.json_response({"error": "Unauthorized"}, status=401)
    return None


async def _runner_data_handler(request):
    """GET /api/runner-data — serve live runner_data.json + action_log.json (Bearer auth)."""
    denied = _check_monitor_auth(request)
    if denied is not None:
        return denied
    import json as _json
    path = os.path.join(DATA_DIR, "runner_data.json")
    try:
        with open(path) as f:
            data = _json.load(f)
    except FileNotFoundError:
        data = {"runners": {}}
    except Exception as e:
        return _web.json_response(
            {"error": f"Could not read runner data: {type(e).__name__}"},
            status=500,
        )

    action_log_path = os.path.join(DATA_DIR, "action_log.json")
    try:
        with open(action_log_path) as f:
            data["action_log"] = _json.load(f)
    except Exception:
        data["action_log"] = []

    return _web.json_response(data)


async def _health_handler(request):
    """GET /api/health — unauthenticated liveness probe."""
    return _web.json_response({"status": "ok", "version": VERSION})


# ── STARTUP ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Runner v{VERSION} online as {bot.user}")
    # One-time, idempotent data cleanup: clear legacy/stuck "Pending Access"
    # records by backfilling access_granted_at. Safe to call every startup —
    # it self-guards via a settings flag and only runs once.
    try:
        migrate_pending_access()
    except Exception as e:
        print(f"[MIGRATE pending_access] ERROR: {type(e).__name__}: {e}", flush=True)
    bot.add_view(XRView(0, True))
    bot.add_view(XRView(0, False))
    bot.add_view(DealerLinkView())
    bot.add_view(LeaderView())
    bot.add_view(ReimburseView("0", 0))
    bot.add_view(DrugAccessView("0"))
    bot.add_view(DoneView())
    bot.add_view(PendingView())
    bot.add_view(ReviewView("0", 0))
    bot.add_view(HelpThreadView())
    bot.add_view(StartThreadView())
    bot.add_view(AllRunnersView())
    bot.add_view(ApprovedSubmissionView("0", 0))
    bot.add_view(PendingAccessView())
    bot.add_view(ReimburseListView())
    bot.add_view(LiveFeedView())
    bot.add_view(DevView())
    bot.add_view(RoleCheckView())
    if not task_module.check_expiry.is_running():
        task_module.check_expiry.start()
    if not task_module.poll_armory_feed.is_running():
        task_module.poll_armory_feed.start()
    if not task_module.submission_alerts.is_running():
        task_module.submission_alerts.start()
    if not task_module.process_scheduled_access.is_running():
        task_module.process_scheduled_access.start()
    if not task_module.check_faction_roles.is_running():
        task_module.check_faction_roles.start()
    try:
        _app = _web.Application()
        _app.router.add_get("/internal/data", _internal_data_handler)
        _app.router.add_get("/api/runner-data", _runner_data_handler)
        _app.router.add_get("/api/health", _health_handler)
        _runner_app = _web.AppRunner(_app)
        await _runner_app.setup()
        # Bind the publicly-routed port (Railway's $PORT) so the monitor API is
        # reachable over the internet.
        site = _web.TCPSite(_runner_app, "0.0.0.0", PUBLIC_PORT)
        await site.start()
        print(f"[RunnerBot] Web server on port {PUBLIC_PORT} "
              f"(monitor API {'ENABLED' if MONITOR_API_KEY else 'DISABLED'})")
        # Also bind the legacy internal port if it differs, for backward compat.
        if INTERNAL_PORT != PUBLIC_PORT:
            try:
                internal_site = _web.TCPSite(_runner_app, "0.0.0.0", INTERNAL_PORT)
                await internal_site.start()
                print(f"[RunnerBot] Also listening on internal port {INTERNAL_PORT}")
            except Exception as e:
                print(f"[RunnerBot] Internal port {INTERNAL_PORT} bind skipped: {e}")
    except Exception as e:
        print(f"[RunnerBot] Web server failed: {e}")


bot.run(TOKEN)
