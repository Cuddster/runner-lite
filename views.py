# views.py — v4.0
# Changelog:
#   4.0 — Credits refactor: approval extends expiry only, no credit manipulation.
#         Fixed modal re-pop for scheduled runners. ManageCredits uses weeks_remaining.
#   3.0 — All get_guild() replaced with fetch_guild() fallback, all get_member() replaced with fetch_member()
#   2.9 — All get_member() replaced with fetch_member() for reliable DMs
#   2.8 — Pending Access button on dashboard, PendingAccessView, auto-extend on re-approval
#   2.7 — Pagination on PendingView, Mark Reimbursed + Dashboard on ApprovedSubmissionView
#   2.6 — GrantAccessModal on approve, calendar confirm, ApprovedSubmissionView, RevertConfirmView
import discord
import os
from sheets import push_approval_to_sheet
from datetime import datetime, timezone, timedelta
from config import (COL_GOLD, COL_GREEN, COL_RED, WEEKLY_QUOTA, REIMBURSEMENT,
                    DRUG_DEALER_ROLE_ID, TEST_IDS, GUILD_ID, CHANNEL_ID)
from data import (load_data, save_data, get_runner, encrypt, decrypt, hash_log, get_week_start,
                  get_dealer_last_grant_time, set_dealer_last_grant_time,
                  extend_access_expiry, weeks_remaining, banked_weeks, can_submit_run, grant_weeks,
                  SOFT_CAP_WEEKS, HARD_CAP_WEEKS, note_access)
from api import verify_torn_key, verify_dealer_key, verify_deposit
from embeds import (build_xr_embed, build_leader_embed_dashboard, build_leader_embed_all,
                    build_leader_embed_pending, build_leader_embed_reimburse,
                    build_leader_embed_access, build_leader_embed_access_pending,
                    build_review_embed, build_live_feed_embed, build_dev_embed,
                    build_calendar_embed, build_approved_submission_embed,
                    get_pending_submissions, get_pending_reimbursements, get_pending_access_runners,
                    count_pending_approval, count_pending_reimburse, count_pending_access,
                    build_role_check_embed)


async def get_guild(client):
    """Reliably fetch the guild — falls back to API if not cached."""
    return client.get_guild(GUILD_ID) or await client.fetch_guild(GUILD_ID)


def is_leader(member: discord.Member):
    if member.id in TEST_IDS:
        return True
    return any(r.id == DRUG_DEALER_ROLE_ID for r in member.roles)


# ── XR VIEWS ───────────────────────────────────────────────────────────────
class XRView(discord.ui.View):
    def __init__(self, discord_id: int, linked: bool):
        super().__init__(timeout=None)
        self.discord_id = discord_id
        if not linked:
            self.add_item(LinkButton())
        self.add_item(SubmitButton(linked))
        self.add_item(AlertToggleButton())
        self.add_item(RefreshXRButton())
        self.add_item(HelpButton())


class LinkButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Link Torn Account", style=discord.ButtonStyle.primary, custom_id="xr_link")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RegisterKeyModal())


class SubmitButton(discord.ui.Button):
    def __init__(self, linked: bool):
        super().__init__(label="Submit Run", style=discord.ButtonStyle.secondary, custom_id="xr_submit",
                         disabled=not linked)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SubmitModal())


class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🆘 Help", style=discord.ButtonStyle.danger, custom_id="xr_help")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "⚠️ This will ping leadership. Are you sure you need assistance?",
            view=HelpConfirmView(interaction.user.id),
            ephemeral=True
        )


class HelpConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id

    @discord.ui.button(label="Yes, contact leadership", style=discord.ButtonStyle.danger, custom_id="help_confirm_yes")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Not your button.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        guild = await get_guild(interaction.client)
        role = guild.get_role(DRUG_DEALER_ROLE_ID) if guild else None
        runner_name = interaction.user.display_name

        parent = interaction.channel.parent if hasattr(interaction.channel, 'parent') else interaction.channel
        if not parent:
            parent = guild.get_channel(CHANNEL_ID)
        if not parent:
            return await interaction.followup.send("Could not find the runner channel.", ephemeral=True)

        thread = await parent.create_thread(
            name=f"Help — {runner_name}",
            type=discord.ChannelType.private_thread,
            invitable=False
        )
        await thread.add_user(interaction.user)
        data = load_data()
        for r in data["runners"].values():
            if not r.get("is_dealer"):
                continue
            try:
                dealer = await guild.fetch_member(int(r["discord_id"]))
                await thread.add_user(dealer)
            except Exception as e:
                print(f"[HELP] Could not add dealer {r.get('discord_id')}: {e}", flush=True)
        role_mention = role.mention if role else "@DrugDealer"
        await thread.send(
            content=role_mention,
            embed=discord.Embed(
                title="🆘 Runner Needs Assistance",
                description=f"**{runner_name}** has requested help. A leader will be with you shortly.",
                colour=0xE53935
            ),
            view=HelpThreadView()
        )
        await interaction.followup.send(f"Help thread created: {thread.mention}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="help_confirm_no")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class HelpThreadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Close Thread", style=discord.ButtonStyle.success, custom_id="help_close")
    async def close_thread(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.channel.send("✅ Issue resolved. Thread closed.")
        await interaction.channel.edit(archived=True)


class RefreshXRButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✈️ Refresh", style=discord.ButtonStyle.secondary, custom_id="xr_refresh")

    async def callback(self, interaction: discord.Interaction):
        data = load_data()
        runner = get_runner(data, interaction.user.id)
        linked = runner.get("torn_id") is not None
        await interaction.response.edit_message(embed=build_xr_embed(runner), view=XRView(interaction.user.id, linked))


class AlertToggleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Toggle Expiry Alert", style=discord.ButtonStyle.secondary, custom_id="xr_alert")

    async def callback(self, interaction: discord.Interaction):
        data = load_data()
        runner = get_runner(data, interaction.user.id)
        runner["expiry_alerts"] = not runner.get("expiry_alerts", False)
        save_data(data)
        embed = build_xr_embed(runner)
        await interaction.message.edit(embed=embed, view=XRView(interaction.user.id, runner.get("torn_id") is not None))
        state = "on" if runner["expiry_alerts"] else "off"
        msg = f"Expiry alerts turned {state}." + (
            " You will be DM'd 24h before access expires." if runner["expiry_alerts"] else "")
        await interaction.response.send_message(msg, ephemeral=True)


# ── DEALER LINK VIEW ───────────────────────────────────────────────────────
class DealerLinkView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Link Torn Account", style=discord.ButtonStyle.primary, custom_id="dl_link")
    async def link(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DealerRegisterModal())


# ── MODALS ─────────────────────────────────────────────────────────────────
class DealerRegisterModal(discord.ui.Modal, title="Link Dealer Torn Account"):
    api_key = discord.ui.TextInput(
        label="API Key (Limited/Custom + Faction access)",
        placeholder="Limited or Custom key with Faction access enabled",
        required=True, min_length=16, max_length=16
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        torn_id, torn_name, err = await verify_dealer_key(self.api_key.value.strip())
        if err:
            return await interaction.followup.send(f"Could not verify key: {err}", ephemeral=True)
        data = load_data()
        runner = get_runner(data, interaction.user.id)
        runner["torn_id"] = torn_id
        runner["torn_name"] = torn_name
        runner["torn_api_key_enc"] = encrypt(self.api_key.value.strip())
        runner["is_dealer"] = True
        save_data(data)

        embed = discord.Embed(
            title="✅ Dealer Account Linked",
            description=f"Linked as **{torn_name}** [{torn_id}] with faction access.",
            colour=COL_GREEN
        )
        await interaction.message.edit(embed=embed, view=None)
        await interaction.followup.send("Account linked.", ephemeral=True)
        await interaction.channel.send(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard", 0))


class RegisterKeyModal(discord.ui.Modal, title="Link Your Torn Account"):
    api_key = discord.ui.TextInput(
        label="Torn API Key (any level)",
        placeholder="Paste your 16-character Torn API key",
        required=True, min_length=16, max_length=16
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        torn_id, torn_name, err = await verify_torn_key(self.api_key.value.strip())
        if err:
            return await interaction.followup.send(f"Could not verify key: {err}", ephemeral=True)
        data = load_data()
        runner = get_runner(data, interaction.user.id)
        runner["torn_id"] = torn_id
        runner["torn_name"] = torn_name
        runner["torn_api_key_enc"] = encrypt(self.api_key.value.strip())
        save_data(data)
        embed = build_xr_embed(runner)
        await interaction.message.edit(embed=embed, view=XRView(interaction.user.id, True))
        await interaction.followup.send(f"Linked as **{torn_name}** [{torn_id}].", ephemeral=True)


class SubmitModal(discord.ui.Modal, title="Submit Xanax Run"):
    deposit_log = discord.ui.TextInput(label="Deposit Log", style=discord.TextStyle.paragraph,
                                       placeholder="Paste your deposit log from the faction armory", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        runner = get_runner(data, interaction.user.id)
        if not runner.get("torn_id"):
            return await interaction.followup.send("Link your Torn account first.", ephemeral=True)

        allowed, block_reason = can_submit_run(runner)
        if not allowed:
            save_data(data)  # persist any lock clear from the refresh above
            weeks = banked_weeks(runner)
            if block_reason == "hard_cap":
                return await interaction.followup.send(
                    f"🔒 You're at **{weeks} weeks banked** — the max is {HARD_CAP_WEEKS}. "
                    f"You can't submit again until that drops below {HARD_CAP_WEEKS}.",
                    ephemeral=True
                )
            return await interaction.followup.send(
                f"🔒 You're currently locked from submitting (banked-weeks cap reached at "
                f"**{weeks} weeks**). You'll be able to submit again once that drops below {SOFT_CAP_WEEKS}.",
                ephemeral=True
            )

        torn_id = runner["torn_id"]
        torn_name = runner.get("torn_name") or "Unknown"
        week_start_ts = int(get_week_start().timestamp())

        import re as _re
        log_text = self.deposit_log.value.strip()
        log_match = _re.search(r'deposited (\d+)x Xanax', log_text)
        log_xanax = int(log_match.group(1)) if log_match else None

        if not log_xanax:
            return await interaction.followup.send(
                "Could not read Xanax amount from your deposit log. Make sure you paste the full log line.",
                ephemeral=True
            )
        if log_xanax < 94:
            return await interaction.followup.send(
                f"Minimum 94 Xanax required. Your log shows {log_xanax}x. Contact leadership if you need assistance.",
                ephemeral=True
            )
        if log_xanax > 94:
            await interaction.followup.send(
                f"⚠️ You deposited {log_xanax}x — only 94 counts toward your credit. Leadership will return the {log_xanax - 94} extra.",
                ephemeral=True
            )

        import re as _re2
        date_match = _re2.search(r'(\d{2}:\d{2}:\d{2}) - (\d{2}/\d{2}/\d{2})', log_text)
        historic_days = None
        if date_match:
            try:
                from datetime import datetime as _dt
                date_str = date_match.group(2)
                time_str = date_match.group(1)
                deposit_dt = _dt.strptime(f"{date_str} {time_str}", "%d/%m/%y %H:%M:%S").replace(tzinfo=timezone.utc)
                now_dt = datetime.now(timezone.utc)
                historic_days = (now_dt - deposit_dt).days
            except Exception:
                pass

        from data import decrypt
        dealer_keys = []
        for r in data["runners"].values():
            if r.get("is_dealer") and r.get("torn_api_key_enc"):
                try:
                    dealer_keys.append((r.get("torn_name", "Unknown"), decrypt(r["torn_api_key_enc"])))
                except Exception:
                    continue

        feed_confirmed = False
        api_error = "No dealer API key available for verification"
        for dealer_name, dealer_key in dealer_keys:
            feed_xanax, err = await verify_deposit(torn_id, week_start_ts, dealer_key)
            if err:
                print(f"[VERIFY] Dealer key for {dealer_name} failed: {err}", flush=True)
                api_error = err
                continue
            print(f"[VERIFY] Verified using {dealer_name}'s key", flush=True)
            api_error = None
            feed_confirmed = feed_xanax is not None and feed_xanax > 0
            break

        import aiohttp
        from tasks import update_feed_from_logs, archive_raw_logs
        for dealer_name, dealer_key in dealer_keys:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://api.torn.com/faction/?selections=armorynews&key={dealer_key}"
                    ) as resp:
                        feed_data = await resp.json()
                if "error" not in feed_data:
                    raw = feed_data.get("armorynews", {})
                    archive_raw_logs(raw)
                    await update_feed_from_logs(raw)
                    break
            except Exception:
                continue

        final_xanax = log_xanax

        if api_error:
            api_status = f"API Error: {api_error} — log shows {log_xanax}x (unverified)"
            self_reported = True
        elif not feed_confirmed:
            api_status = f"Not found in armory feed — log shows {log_xanax}x (unverified)"
            self_reported = True
        else:
            api_status = f"Verified — deposit confirmed in armory feed. Log: {log_xanax}x"
            self_reported = False

        dep_hash = hash_log(self.deposit_log.value)

        for prev in runner["submissions"]:
            if prev.get("deposit_log_hash") == dep_hash and prev.get("status") != "rejected":
                return await interaction.followup.send(
                    "This log has already been submitted. Duplicate or edited logs are not accepted.",
                    ephemeral=True
                )

        runner["submissions"].append({
            "id": len(runner["submissions"]) + 1,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "xanax_deposited": final_xanax,
            "api_status": api_status,
            "deposit_log": self.deposit_log.value,
            "deposit_log_hash": dep_hash,
            "log_mismatch": False,
            "self_reported": self_reported,
            "partial": False,
            "historic": historic_days is not None and historic_days > 7,
            "historic_days": historic_days,
            "status": "pending",
            "reimbursed": False,
            "credits_earned": 0
        })
        save_data(data)

        runner = load_data()["runners"][str(interaction.user.id)]
        await interaction.message.edit(embed=build_xr_embed(runner), view=XRView(interaction.user.id, True))

        print(f"[DM] Starting dealer DM process", flush=True)
        pa = count_pending_approval()
        try:
            guild = await get_guild(interaction.client)
            print(f"[DM] guild={guild}", flush=True)
            data2 = load_data()
            dealers = [r for r in data2["runners"].values() if r.get("is_dealer")]
            print(f"[DM] Found {len(dealers)} dealers", flush=True)
            for r in dealers:
                try:
                    member = await guild.fetch_member(int(r["discord_id"]))
                    await member.send(
                        embed=discord.Embed(
                            title="New Submission",
                            description=f"**{torn_name}** submitted a run. {pa} pending approval.",
                            colour=0xFFA726
                        )
                    )
                    print(f"[DM] Sent to dealer {r.get('discord_id')}", flush=True)
                except Exception as e:
                    print(f"[DM ERROR] dealer {r.get('discord_id')}: {type(e).__name__}: {e}", flush=True)
        except Exception as e:
            print(f"[DM ERROR] guild fetch failed: {type(e).__name__}: {e}", flush=True)

        await interaction.followup.send("Your submission has been sent.", ephemeral=True)


class RejectModal(discord.ui.Modal, title="Reject Submission"):
    reason = discord.ui.TextInput(
        label="Reason (optional)", style=discord.TextStyle.paragraph,
        placeholder="Leave blank or explain why this submission was rejected",
        required=False, max_length=500
    )

    def __init__(self, runner_discord_id: str, sub_index: int, message: discord.Message):
        super().__init__()
        self.runner_discord_id = runner_discord_id
        self.sub_index = sub_index
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        sub["status"] = "rejected"
        sub["rejected_at"] = datetime.now(timezone.utc).isoformat()
        sub["rejected_by"] = interaction.user.display_name
        sub["rejection_reason"] = self.reason.value.strip() if self.reason.value else None
        save_data(data)

        reason_text = f"\n> {sub['rejection_reason']}" if sub["rejection_reason"] else ""
        try:
            guild = await get_guild(interaction.client)
            channel = guild.get_channel(CHANNEL_ID)
            runner_thread = None
            if channel:
                torn_name = runner.get("torn_name", "")
                torn_id = runner.get("torn_id", "")
                thread_name = f"XR — {torn_name} [{torn_id}]"
                for thread in channel.threads:
                    if thread.name == thread_name:
                        runner_thread = thread
                        break
            if runner_thread:
                await runner_thread.send(
                    content=f"<@{self.runner_discord_id}> Your submission #{sub['id']} was rejected.{reason_text}\n\nIf you submitted a partial amount and would like a refund of your Xanax, please use the **🆘 Help** button in your dashboard to contact leadership."
                )
        except Exception as e:
            print(f"[REJECT] Could not post to runner thread: {type(e).__name__}: {e}", flush=True)
        try:
            guild = await get_guild(interaction.client)
            member = await guild.fetch_member(int(self.runner_discord_id))
            dm = f"❌ **Your Xanax run submission #{sub['id']} was rejected.**"
            if sub["rejection_reason"]:
                dm += f"\nReason: {sub['rejection_reason']}"
            dm += "\nPlease resubmit with the correct deposit log."
            await member.send(dm)
        except Exception as e:
            print(f"[DM ERROR] reject: {type(e).__name__}: {e}", flush=True)
        await self.message.edit(embed=build_leader_embed_pending(), view=PendingView())
        await interaction.response.send_message("Rejected.", ephemeral=True)


# ── APPROVAL FLOW VIEWS ────────────────────────────────────────────────────
class ReimburseView(discord.ui.View):
    def __init__(self, runner_discord_id: str, sub_index: int):
        super().__init__(timeout=None)
        self.runner_discord_id = runner_discord_id
        self.sub_index = sub_index

    @discord.ui.button(label="Mark Reimbursed", style=discord.ButtonStyle.secondary, custom_id="rv_reimburse")
    async def mark_reimbursed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        torn_id = runner.get("torn_id", "")
        owed = sub.get("credits_earned", 0) * REIMBURSEMENT
        sub["reimbursed"] = True
        sub["reimbursed_by"] = interaction.user.display_name
        sub["reimbursed_at"] = datetime.now(timezone.utc).isoformat()
        save_data(data)
        ticket_id = sub.get("sheet_ticket_id")
        if ticket_id is not None:
            try:
                from sheets import update_paid_status_in_sheet
                await update_paid_status_in_sheet(ticket_id)
            except Exception as e:
                print(f"[SHEETS] update paid error: {type(e).__name__}: {e}", flush=True)
        reimburse_url = f"https://www.torn.com/factions.php?step=your#/tab=controls&addMoneyTo={torn_id}&money={int(owed)}"
        approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
        await interaction.message.edit(embed=approved_embed, view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index))
        await interaction.response.send_message(
            f"✅ **{runner.get('torn_name', 'Runner')}** marked as reimbursed. [💰 Send ${owed:,.0f} on Torn]({reimburse_url})",
            ephemeral=True
        )
  
    @discord.ui.button(label="← Back to Dashboard", style=discord.ButtonStyle.secondary, custom_id="rv_back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))


# ── DRUG ACCESS VIEW (kept for backward compat) ────────────────────────────
class DrugAccessView(discord.ui.View):
    def __init__(self, runner_discord_id: str):
        super().__init__(timeout=None)
        self.runner_discord_id = runner_discord_id

    @discord.ui.button(label="Grant Drug Access Now", style=discord.ButtonStyle.success, custom_id="da_grant")
    async def grant_drug_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        now = datetime.now(timezone.utc)
        runner["armory_access"] = True
        if not runner.get("access_granted_at"):
            runner["access_granted_at"] = now.isoformat()
        runner.pop("access_scheduled_for", None)
        note_access(runner, interaction.user.display_name, "grant drug access now")
        save_data(data)
        ARMORY_URL = "https://www.torn.com/factions.php?step=your&type=1#/tab=controls&option=members"
        embed = interaction.message.embeds[0]
        embed.set_footer(text=f"Drug access granted by {interaction.user.display_name} — {now.strftime('%b %d, %Y %H:%M UTC')}")
        await interaction.message.edit(embed=embed, view=DoneView())
        await interaction.response.send_message(
            f"✅ Access granted now. Remember to toggle drug access in-game: {ARMORY_URL}", ephemeral=True
        )
        try:
            guild = await get_guild(interaction.client)
            member = await guild.fetch_member(int(self.runner_discord_id))
            expiry = runner.get("access_expires")
            exp_str = f" Expires **{datetime.fromisoformat(expiry).strftime('%b %d, %Y')}**." if expiry else ""
            await member.send(f"🎉 Your armory drug access has been granted!{exp_str}")
        except Exception as e:
            print(f"[DM ERROR] grant_drug_access: {type(e).__name__}: {e}", flush=True)

    @discord.ui.button(label="📅 Schedule Access", style=discord.ButtonStyle.secondary, custom_id="da_schedule")
    async def schedule_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.send_modal(ScheduleAccessModal(self.runner_discord_id, interaction.message))

    @discord.ui.button(label="← Back to Dashboard", style=discord.ButtonStyle.secondary, custom_id="da_back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))


class ScheduleAccessModal(discord.ui.Modal, title="Schedule Drug Access"):
    scheduled_time = discord.ui.TextInput(
        label="Date/Time in UTC (Torn Time) — YYYY-MM-DD HH:MM",
        placeholder="e.g. 2026-05-11 00:00",
        required=True, min_length=16, max_length=16
    )

    def __init__(self, runner_discord_id: str, message: discord.Message):
        super().__init__()
        self.runner_discord_id = runner_discord_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            scheduled_dt = datetime.strptime(self.scheduled_time.value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return await interaction.followup.send(
                "Invalid format. Use `YYYY-MM-DD HH:MM` e.g. `2026-05-11 00:00`", ephemeral=True
            )
        if scheduled_dt <= datetime.now(timezone.utc):
            return await interaction.followup.send("Scheduled time must be in the future.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        runner["access_scheduled_for"] = scheduled_dt.isoformat()
        note_access(runner, interaction.user.display_name, f"access scheduled for {scheduled_dt:%b %d %H:%M} UTC")
        save_data(data)
        embed = self.message.embeds[0]
        embed.set_footer(text=f"Access scheduled for {scheduled_dt.strftime('%b %d, %Y %H:%M UTC')} by {interaction.user.display_name}")
        await self.message.edit(embed=embed, view=DoneView())
        await interaction.followup.send(
            f"✅ Access scheduled for **{scheduled_dt.strftime('%b %d, %Y %H:%M UTC')}** (Torn Time).", ephemeral=True
        )
        try:
            guild = await get_guild(interaction.client)
            member = await guild.fetch_member(int(self.runner_discord_id))
            await member.send(
                f"📅 Your armory drug access has been scheduled for **{scheduled_dt.strftime('%b %d, %Y %H:%M UTC')}** (Torn Time)."
            )
        except Exception as e:
            print(f"[DM ERROR] schedule_access: {type(e).__name__}: {e}", flush=True)


class DoneView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="← Back to Dashboard", style=discord.ButtonStyle.secondary, custom_id="done_back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))


# ── GRANT ACCESS MODAL ─────────────────────────────────────────────────────
class GrantAccessModal(discord.ui.Modal, title="Set Access Grant Time"):
    grant_time = discord.ui.TextInput(
        label="Date & Time (UTC/Torn Time) — DD/MM/YY HH:MM",
        placeholder="e.g. 10/05/26 18:00",
        required=True,
        min_length=14,
        max_length=14,
    )

    def __init__(
        self,
        runner_discord_id: str,
        sub_index: int,
        message: discord.Message,
        dealer_discord_id: int,
        is_partial: bool = False,
        prefill_dt: datetime | None = None,
        existing_scheduled: str | None = None,
    ):
        super().__init__()
        self.runner_discord_id = str(runner_discord_id)
        self.sub_index = sub_index
        self.message = message
        self.dealer_discord_id = dealer_discord_id
        self.is_partial = is_partial
        self.existing_scheduled = existing_scheduled

        if prefill_dt:
            self.grant_time.default = prefill_dt.strftime("%d/%m/%y %H:%M")
        else:
            last = get_dealer_last_grant_time(dealer_discord_id)
            if last:
                self.grant_time.default = last

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.grant_time.value.strip()
        try:
            scheduled_dt = datetime.strptime(raw, "%d/%m/%y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Invalid format. Use `DD/MM/YY HH:MM` e.g. `10/05/26 18:00`",
                ephemeral=True
            )

        if scheduled_dt <= datetime.now(timezone.utc):
            data = load_data()
            runner = get_runner(data, int(self.runner_discord_id))
            weeks = grant_weeks(runner, runner["submissions"][self.sub_index], scheduled_dt)   # read before the schedule is cleared
            runner["armory_access"] = True
            runner["access_granted_at"] = scheduled_dt.isoformat()
            runner.pop("access_scheduled_for", None)
            # Set expiry from grant time + credits
            sub = runner["submissions"][self.sub_index]
            credits_earned = sub.get("credits_earned", 1)
            new_expiry = scheduled_dt + timedelta(days=7 * weeks)
            runner["access_expires"] = new_expiry.isoformat()
            note_access(runner, interaction.user.display_name, f"access granted from {scheduled_dt:%b %d %H:%M} UTC")
            save_data(data)
            set_dealer_last_grant_time(self.dealer_discord_id, raw)
            approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
            await interaction.response.send_message(
                f"⚠️ That time is in the past — access has been granted immediately instead.",
                ephemeral=True
            )
            try:
                msg = self.message
                await msg.edit(embed=approved_embed, view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index))
            except Exception:
                pass
            return

        cal_embed = build_calendar_embed(scheduled_dt, interaction.user.display_name)

        if self.existing_scheduled:
            try:
                old_dt = datetime.fromisoformat(self.existing_scheduled).replace(tzinfo=timezone.utc)
                old_str = old_dt.strftime("%d %b %Y %H:%M")
                cal_embed.description = (
                    f"⚠️ **Currently scheduled for {old_str} UTC — this will replace it.**\n\n"
                    + (cal_embed.description or "")
                )
            except Exception:
                pass

        await interaction.response.send_message(
            embed=cal_embed,
            view=ScheduleConfirmView(
                runner_discord_id=self.runner_discord_id,
                sub_index=self.sub_index,
                message=self.message,
                dealer_discord_id=self.dealer_discord_id,
                scheduled_dt=scheduled_dt,
                raw_time_str=raw,
                is_partial=self.is_partial,
            ),
            ephemeral=True
        )


# ── SCHEDULE CONFIRM VIEW ──────────────────────────────────────────────────
class ScheduleConfirmView(discord.ui.View):
    def __init__(
        self,
        runner_discord_id: str,
        sub_index: int,
        message: discord.Message,
        dealer_discord_id: int,
        scheduled_dt: datetime,
        raw_time_str: str,
        is_partial: bool = False,
    ):
        super().__init__(timeout=300)
        self.runner_discord_id = str(runner_discord_id)
        self.sub_index = sub_index
        self.message = message
        self.dealer_discord_id = dealer_discord_id
        self.scheduled_dt = scheduled_dt
        self.raw_time_str = raw_time_str
        self.is_partial = is_partial

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success, custom_id="sc_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.dealer_discord_id and not is_leader(interaction.user):
            return await interaction.response.send_message("Not your button.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        weeks = grant_weeks(runner, runner["submissions"][self.sub_index], self.scheduled_dt)   # read before the schedule is overwritten
        runner["access_scheduled_for"] = self.scheduled_dt.isoformat()
        # Set expiry based on scheduled start time + 7 days × credits
        sub = runner["submissions"][self.sub_index]
        credits_earned = sub.get("credits_earned", 1)
        new_expiry = self.scheduled_dt + timedelta(days=7 * weeks)
        runner["access_expires"] = new_expiry.isoformat()
        note_access(runner, interaction.user.display_name, f"access scheduled for {self.scheduled_dt:%b %d %H:%M} UTC")
        save_data(data)

        set_dealer_last_grant_time(self.dealer_discord_id, self.raw_time_str)

        approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
        await self.message.edit(
            embed=approved_embed,
            view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index)
        )

        try:
            guild = await get_guild(interaction.client)
            member = await guild.fetch_member(int(self.runner_discord_id))
            await member.send(
                f"📅 Your armory drug access has been scheduled for "
                f"**{self.scheduled_dt.strftime('%d %b %Y at %H:%M')} UTC** (Torn Time)."
            )
        except Exception as e:
            print(f"[DM ERROR] schedule_confirm: {type(e).__name__}: {e}", flush=True)

        await interaction.followup.send(
            f"✅ Access scheduled for **{self.scheduled_dt.strftime('%d %b %Y %H:%M UTC')}**.",
            ephemeral=True
        )

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.secondary, custom_id="sc_edit")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.dealer_discord_id and not is_leader(interaction.user):
            return await interaction.response.send_message("Not your button.", ephemeral=True)

        await interaction.response.send_modal(
            GrantAccessModal(
                runner_discord_id=self.runner_discord_id,
                sub_index=self.sub_index,
                message=self.message,
                dealer_discord_id=self.dealer_discord_id,
                is_partial=self.is_partial,
                prefill_dt=self.scheduled_dt,
            )
        )


# ── APPROVED SUBMISSION VIEW ───────────────────────────────────────────────
class ApprovedSubmissionView(discord.ui.View):
    def __init__(self, runner_discord_id: str, sub_index: int):
        super().__init__(timeout=None)
        self.runner_discord_id = str(runner_discord_id)
        self.sub_index = sub_index

    @discord.ui.button(label="🎉 Grant Now", style=discord.ButtonStyle.success, custom_id="asv_grant_now", row=0)
    async def grant_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        now = datetime.now(timezone.utc)
        runner["armory_access"] = True
        runner["access_granted_at"] = now.isoformat()
        runner.pop("access_scheduled_for", None)
        # Safety net: if expiry is missing, set it from now + credits
        if not runner.get("access_expires"):
            sub = runner["submissions"][self.sub_index]
            credits_earned = sub.get("credits_earned", 1)
            new_expiry = now + timedelta(days=7 * credits_earned)
            runner["access_expires"] = new_expiry.isoformat()
        note_access(runner, interaction.user.display_name, "grant now")
        save_data(data)
        approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
        await interaction.message.edit(embed=approved_embed, view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index))
        ARMORY_URL = "https://www.torn.com/factions.php?step=your&type=1#/tab=controls&option=members"
        await interaction.followup.send(
            f"✅ Access granted now. Toggle in-game: {ARMORY_URL}", ephemeral=True
        )
        try:
            guild = await get_guild(interaction.client)
            member = await guild.fetch_member(int(self.runner_discord_id))
            expiry = runner.get("access_expires")
            exp_str = f" Expires **{datetime.fromisoformat(expiry).strftime('%b %d, %Y')}**." if expiry else ""
            await member.send(f"🎉 Your armory drug access has been granted!{exp_str}")
        except Exception as e:
            print(f"[DM ERROR] grant_now: {type(e).__name__}: {e}", flush=True)

    @discord.ui.button(label="✅ Mark Reimbursed", style=discord.ButtonStyle.success, custom_id="asv_reimburse", row=1)
    async def mark_reimbursed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        if sub.get("reimbursed"):
            return await interaction.response.send_message("Already marked as reimbursed.", ephemeral=True)
        torn_id = runner.get("torn_id", "")
        owed = sub.get("credits_earned", 0) * REIMBURSEMENT
        sub["reimbursed"] = True
        sub["reimbursed_by"] = interaction.user.display_name
        sub["reimbursed_at"] = datetime.now(timezone.utc).isoformat()
        save_data(data)
        ticket_id = sub.get("sheet_ticket_id")
        if ticket_id is not None:
            try:
                from sheets import update_paid_status_in_sheet
                await update_paid_status_in_sheet(ticket_id)
            except Exception as e:
                print(f"[SHEETS] update paid error: {type(e).__name__}: {e}", flush=True)
        reimburse_url = f"https://www.torn.com/factions.php?step=your#/tab=controls&addMoneyTo={torn_id}&money={int(owed)}"
        approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
        await interaction.message.edit(embed=approved_embed, view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index))
        await interaction.response.send_message(
            f"✅ **{runner.get('torn_name', 'Runner')}** marked as reimbursed. [💰 Send ${owed:,.0f} on Torn]({reimburse_url})",
            ephemeral=True
        )
        try:
            guild = await get_guild(interaction.client)
            member = await guild.fetch_member(int(self.runner_discord_id))
            await member.send(
                f"💰 **Your reimbursement of ${owed:,.0f} has been sent!**\n"
                f"Paid by {interaction.user.display_name}."
            )
        except Exception as e:
            print(f"[DM ERROR] mark_reimbursed: {type(e).__name__}: {e}", flush=True)

    @discord.ui.button(label="⏰ Set Access Time", style=discord.ButtonStyle.primary, custom_id="asv_set_time", row=1)
    async def set_access_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)

        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        existing = runner.get("access_scheduled_for")
        prefill_dt = None
        if existing:
            try:
                prefill_dt = datetime.fromisoformat(existing).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        sub = runner["submissions"][self.sub_index]
        is_partial = sub.get("xanax_deposited", 0) < WEEKLY_QUOTA

        await interaction.response.send_modal(
            GrantAccessModal(
                runner_discord_id=self.runner_discord_id,
                sub_index=self.sub_index,
                message=interaction.message,
                dealer_discord_id=interaction.user.id,
                is_partial=is_partial,
                prefill_dt=prefill_dt,
                existing_scheduled=existing,
            )
        )

    @discord.ui.button(label="↩ Revert Approval", style=discord.ButtonStyle.danger, custom_id="asv_revert", row=2)
    async def revert_approval(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)

        await interaction.response.send_message(
            embed=discord.Embed(
                title="↩ Revert Approval",
                description=(
                    "This will return the submission to **pending** status.\n\n"
                    "Any scheduled or granted armory access will be silently cancelled.\n\n"
                    "**Are you sure?**"
                ),
                colour=COL_RED
            ),
            view=RevertConfirmView(self.runner_discord_id, self.sub_index, interaction.message),
            ephemeral=True
        )

    @discord.ui.button(label="⏭ Next Pending", style=discord.ButtonStyle.primary, custom_id="asv_next_pending", row=2)
    async def next_pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        pending = get_pending_submissions()
        if not pending:
            return await interaction.response.send_message("No more pending submissions.", ephemeral=True)
        discord_id, sub_index, r, s = pending[0]
        embed = build_review_embed(discord_id, sub_index)
        await interaction.response.defer()
        await interaction.message.edit(embed=embed, view=ReviewView(discord_id, sub_index))
  
    @discord.ui.button(label="← Back to Dashboard", style=discord.ButtonStyle.secondary, custom_id="asv_back", row=2)
    async def back_to_dashboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))


# ── REVERT CONFIRM VIEW ────────────────────────────────────────────────────
class RevertConfirmView(discord.ui.View):
    def __init__(self, runner_discord_id: str, sub_index: int, message: discord.Message):
        super().__init__(timeout=60)
        self.runner_discord_id = str(runner_discord_id)
        self.sub_index = sub_index
        self.message = message

    @discord.ui.button(label="Yes, Revert", style=discord.ButtonStyle.danger, custom_id="rc_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        now = datetime.now(timezone.utc)

        credits_to_restore = sub.get("credits_earned", 0)
        xanax_deposited = sub.get("xanax_deposited", 0)

        sub["status"] = "pending"
        sub["reverted_at"] = now.isoformat()
        sub["reverted_by"] = interaction.user.display_name
        sub["approved_at"] = None
        sub["approved_by"] = None
        sub["reimbursed"] = False
        sub["reimbursed_at"] = None
        sub["reimbursed_by"] = None
        sub["credits_earned"] = 0

        runner["total_xanax"] = max(0, runner.get("total_xanax", 0) - xanax_deposited)

        if credits_to_restore > 0 and runner.get("access_expires"):
            try:
                curr_exp = datetime.fromisoformat(runner["access_expires"]).replace(tzinfo=timezone.utc)
                new_exp = curr_exp - timedelta(days=7 * credits_to_restore)
                runner["access_expires"] = None if new_exp <= now else new_exp.isoformat()
            except Exception:
                runner["access_expires"] = None

        runner.pop("access_scheduled_for", None)
        if not runner.get("access_expires"):   # only switch off when no paid time is left
            runner["armory_access"] = False
        note_access(runner, interaction.user.display_name, f"approval reverted (-{credits_to_restore} week)")
        # NOTE: we intentionally KEEP access_granted_at as a historical marker.
        # Reverting this submission already flips its status back to "pending"
        # (above), so it leaves the Pending Access queue on its own. Preserving
        # the grant timestamp means that if this runner is later re-approved,
        # the newer approved_at correctly re-surfaces them in Pending Access,
        # while old/already-handled approvals never boomerang back.

        save_data(data)

        await self.message.edit(
            embed=build_review_embed(self.runner_discord_id, self.sub_index),
            view=ReviewView(self.runner_discord_id, self.sub_index)
        )
        await interaction.followup.send(
            "↩ Submission reverted to **pending**. Access cancelled.", ephemeral=True
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="rc_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


# ── PENDING ACCESS VIEW ────────────────────────────────────────────────────
class PendingAccessView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        runners = get_pending_access_runners()
        for idx, (discord_id, runner) in enumerate(runners[:4]):
            name = runner.get("torn_name") or "Unknown"
            grant_btn = discord.ui.Button(
                label=f"⏰ {name}",
                style=discord.ButtonStyle.primary,
                custom_id=f"pac_{discord_id}",
                row=idx
            )
            grant_btn.callback = self._make_grant_callback(discord_id, runner)
            self.add_item(grant_btn)

            dismiss_btn = discord.ui.Button(
                label=f"❌ {name}",
                style=discord.ButtonStyle.danger,
                custom_id=f"pac_dismiss_{discord_id}",
                row=idx
            )
            dismiss_btn.callback = self._make_dismiss_callback(discord_id, runner)
            self.add_item(dismiss_btn)

        back = discord.ui.Button(label="← Dashboard", style=discord.ButtonStyle.secondary, custom_id="pac_back", row=4)
        back.callback = self._back_callback
        self.add_item(back)

    def _make_grant_callback(self, discord_id, runner):
        async def callback(interaction: discord.Interaction):
            if not is_leader(interaction.user):
                return await interaction.response.send_message("Leadership only.", ephemeral=True)
            data = load_data()
            r = data["runners"].get(str(discord_id), {})
            sub_index = None
            for i, s in enumerate(r.get("submissions", [])):
                if s.get("status") == "approved":
                    sub_index = i
            if sub_index is None:
                return await interaction.response.send_message("No approved submission found.", ephemeral=True)

            existing = r.get("access_scheduled_for")
            prefill_dt = None
            if existing:
                try:
                    prefill_dt = datetime.fromisoformat(existing).replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            await interaction.response.send_modal(
                GrantAccessModal(
                    runner_discord_id=str(discord_id),
                    sub_index=sub_index,
                    message=interaction.message,
                    dealer_discord_id=interaction.user.id,
                    prefill_dt=prefill_dt,
                    existing_scheduled=existing,
                )
            )
        return callback

    def _make_dismiss_callback(self, discord_id, runner):
        async def callback(interaction: discord.Interaction):
            if not is_leader(interaction.user):
                return await interaction.response.send_message("Leadership only.", ephemeral=True)
            name = runner.get("torn_name") or "Unknown"
            tid = runner.get("torn_id", "?")
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Remove from Pending Access",
                    description=(
                        f"Remove **{name} [{tid}]** from the pending access list?\n\n"
                        f"They'll reappear automatically if they submit a new run."
                    ),
                    colour=COL_RED
                ),
                view=DismissAccessConfirmView(str(discord_id), interaction.message),
                ephemeral=True
            )
        return callback
    async def _back_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))

# ── DISMISS ACCESS CONFIRM VIEW ───────────────────────────────────────────
class DismissAccessConfirmView(discord.ui.View):
    def __init__(self, runner_discord_id: str, message: discord.Message):
        super().__init__(timeout=60)
        self.runner_discord_id = str(runner_discord_id)
        self.message = message

    @discord.ui.button(label="Yes, Remove", style=discord.ButtonStyle.danger, custom_id="dac_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        runner["access_dismissed"] = True
        note_access(runner, interaction.user.display_name, "removed from Pending Access")
        save_data(data)
        name = runner.get("torn_name") or "Unknown"
        await self.message.edit(
            embed=build_leader_embed_access_pending(),
            view=PendingAccessView()
        )
        await interaction.followup.send(f"✅ **{name}** removed from pending access.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="dac_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)

# ── PENDING / REVIEW VIEWS ─────────────────────────────────────────────────
class PendingView(discord.ui.View):
    def __init__(self, page: int = 0):
        super().__init__(timeout=None)
        self.page = page
        pending = get_pending_submissions()
        page_size = 4
        total = len(pending)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = page * page_size
        page_items = pending[start:start + page_size]

        for discord_id, sub_index, runner, sub in page_items:
            name = runner.get("torn_name") or "Unknown"
            btn = discord.ui.Button(
                label=f"Review {name}",
                style=discord.ButtonStyle.primary,
                custom_id=f"rv_{discord_id}_{sub_index}"
            )
            btn.callback = self._make_review_callback(discord_id, sub_index)
            self.add_item(btn)

        prev_btn = discord.ui.Button(
            label="← Prev",
            style=discord.ButtonStyle.secondary,
            custom_id="pv_prev",
            row=4,
            disabled=page == 0
        )
        prev_btn.callback = self._prev_callback
        self.add_item(prev_btn)

        page_indicator = discord.ui.Button(
            label=f"{page + 1} / {total_pages}",
            style=discord.ButtonStyle.secondary,
            custom_id="pv_page",
            row=4,
            disabled=True
        )
        self.add_item(page_indicator)

        next_btn = discord.ui.Button(
            label="Next →",
            style=discord.ButtonStyle.secondary,
            custom_id="pv_next",
            row=4,
            disabled=page >= total_pages - 1
        )
        next_btn.callback = self._next_callback
        self.add_item(next_btn)

        back = discord.ui.Button(label="← Dashboard", style=discord.ButtonStyle.secondary, custom_id="pv_back", row=4)
        back.callback = self._back_callback
        self.add_item(back)

    def _make_review_callback(self, discord_id, sub_index):
        async def callback(interaction: discord.Interaction):
            if not is_leader(interaction.user):
                return await interaction.response.send_message("Leadership only.", ephemeral=True)
            await interaction.response.defer()
            await interaction.message.edit(
                embed=build_review_embed(discord_id, sub_index),
                view=ReviewView(discord_id, sub_index)
            )
        return callback

    async def _prev_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_pending(self.page - 1), view=PendingView(self.page - 1))

    async def _next_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_pending(self.page + 1), view=PendingView(self.page + 1))

    async def _back_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))

class LateApproveModal(discord.ui.Modal, title="Late Approval — Mid-Week Access"):
    confirm_field = discord.ui.TextInput(
        label='Type CONFIRM to grant mid-week access',
        placeholder='Access will expire the coming Sunday 00:00 UTC',
        required=True,
        max_length=7,
    )

    def __init__(self, runner_discord_id, sub_index, message):
        super().__init__()
        self.runner_discord_id = str(runner_discord_id)
        self.sub_index = sub_index
        self.message = message

    async def on_submit(self, interaction):
        if self.confirm_field.value.strip().upper() != "CONFIRM":
            return await interaction.response.send_message("Cancelled.", ephemeral=True)

        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        if sub["status"] != "pending":
            return await interaction.response.send_message("Already processed.", ephemeral=True)

        has_access = runner.get("armory_access") or runner.get("access_scheduled_for")
        if has_access:
            await interaction.response.defer()

    
        now = datetime.now(timezone.utc)
        days_until_sun = (6 - now.weekday()) % 7
        if days_until_sun == 0:
            days_until_sun = 7
        next_sunday = (now + timedelta(days=days_until_sun)).replace(hour=0, minute=0, second=0, microsecond=0)

        sub["status"] = "approved"
        sub["credits_earned"] = 1
        sub["approved_at"] = now.isoformat()
        sub["approved_by"] = interaction.user.display_name
        runner["total_xanax"] += sub["xanax_deposited"]
        runner.pop("access_dismissed", None)
        runner["armory_access"] = True
        runner["access_granted_at"] = now.isoformat()
        runner["access_expires"] = next_sunday.isoformat()
        runner["expiry_warning_sent"] = False
        note_access(runner, interaction.user.display_name, "late approve — access now until Sunday")

        save_data(data)
        try:
            await push_approval_to_sheet(runner, sub)
            data = load_data()
            runner = data["runners"][self.runner_discord_id]
            runner["submissions"][self.sub_index]["sheet_ticket_id"] = sub.get("sheet_ticket_id")
            save_data(data)
        except Exception as e:
            print(f"[SHEETS] approval push error: {type(e).__name__}: {e}", flush=True)

        days_left = (next_sunday - now).days
        approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
        await self.message.edit(embed=approved_embed, view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index))
        await interaction.response.send_message(
            f"⏰ Late approval for **{runner.get('torn_name', 'Unknown')}**.\n"
            f"Access active now — expires **{next_sunday.strftime('%b %d')}** ({days_left} days).\n"
            f"Reimbursement still needed.",
            ephemeral=False,
        )

class ReviewView(discord.ui.View):
    def __init__(self, runner_discord_id: str, sub_index: int):
        super().__init__(timeout=None)
        self.runner_discord_id = str(runner_discord_id)
        self.sub_index = sub_index

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="rev_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        if sub["status"] != "pending":
            return await interaction.response.send_message("Already processed.", ephemeral=True)

        has_access = runner.get("armory_access") or runner.get("access_scheduled_for")
        await interaction.response.defer()

        sub["status"] = "approved"
        sub["approved_at"] = datetime.now(timezone.utc).isoformat()
        sub["approved_by"] = interaction.user.display_name
        credits_earned = 1
        sub["credits_earned"] = credits_earned
        runner["total_xanax"] += sub["xanax_deposited"]
        runner.pop("access_dismissed", None)   # a new approval always re-surfaces in Pending Accessrunner.pop("access_dismissed", None)   # a new approval always re-surfaces in Pending Access

        now = datetime.now(timezone.utc)
        new_expiry = extend_access_expiry(runner, credits_earned)
        note_access(runner, interaction.user.display_name, "approved deposit (+1 week)")
        has_access = runner.get("armory_access") or runner.get("access_scheduled_for")
        is_partial = False

        current_weeks = banked_weeks(runner)
        newly_locked = False
        if current_weeks > SOFT_CAP_WEEKS and not runner.get("weeks_banked_locked"):
            runner["weeks_banked_locked"] = True
            newly_locked = True

        save_data(data)
        try:
            await push_approval_to_sheet(runner, sub)
            data = load_data()
            data["runners"][self.runner_discord_id]["submissions"][self.sub_index]["sheet_ticket_id"] = sub.get("sheet_ticket_id")
            save_data(data)
        except Exception as e:
            print(f"[SHEETS] approval push error: {type(e).__name__}: {e}", flush=True)
        try:
            guild = await get_guild(interaction.client)
            member = await guild.fetch_member(int(self.runner_discord_id))
            if new_expiry:
                expiry_line = f"Access expires **{new_expiry.strftime('%b %d, %Y')}**. "
            else:
                expiry_line = "Access start date pending. "
            await member.send(
                f"✅ **Your Xanax run has been approved!**\n"
                f"**{sub['xanax_deposited']}x Xanax** — {credits_earned} week(s) of armory access granted.\n"
                f"{expiry_line}Reimbursement incoming."
            )
        except Exception as e:
            print(f"[DM ERROR] approve: {type(e).__name__}: {e}", flush=True)

        if newly_locked:
            thread_id = runner.get("thread_id")
            if thread_id:
                try:
                    guild2 = await get_guild(interaction.client)
                    thread = await guild2.fetch_channel(int(thread_id))
                    await thread.send(
                        f"🔒 **Banked-weeks cap reached.** You're now at **{current_weeks} weeks** "
                        f"banked (soft cap is {SOFT_CAP_WEEKS}). You won't be able to submit another "
                        f"run until that drops back below {SOFT_CAP_WEEKS} — it ticks down automatically "
                        f"as your access expires."
                    )
                except Exception as e:
                    print(f"[LOCK NOTICE ERROR] {type(e).__name__}: {e}", flush=True)

        if has_access:
            approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
            await interaction.message.edit(
                embed=approved_embed,
                view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index)
            )
        else:
            approved_embed = build_approved_submission_embed(self.runner_discord_id, self.sub_index)
            await interaction.message.edit(
                embed=approved_embed,
                view=ApprovedSubmissionView(self.runner_discord_id, self.sub_index)
            )

    @discord.ui.button(label="⏰ Late Approve", style=discord.ButtonStyle.secondary, custom_id="rev_late")
    async def late_approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        if sub["status"] != "pending":
            return await interaction.response.send_message("Already processed.", ephemeral=True)
        await interaction.response.send_modal(
            LateApproveModal(self.runner_discord_id, self.sub_index, interaction.message)
        )  
  
    @discord.ui.button(label="🔄 Re-verify", style=discord.ButtonStyle.secondary, custom_id="rev_reverify")
    async def reverify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        torn_id = runner.get("torn_id")

        from data import get_week_start
        try:
            submitted_dt = datetime.fromisoformat(sub["submitted_at"]).replace(tzinfo=__import__("datetime").timezone.utc)
            days_since_sat = (submitted_dt.weekday() - 5) % 7
            week_start_dt = (submitted_dt - __import__("datetime").timedelta(days=days_since_sat)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            week_start_ts = int(week_start_dt.timestamp())
        except Exception:
            week_start_dt = get_week_start()
            week_start_ts = int(week_start_dt.timestamp())
        week_end_dt = week_start_dt + __import__("datetime").timedelta(days=7)

        dealer_key = None
        for r in data["runners"].values():
            if r.get("is_dealer") and r.get("torn_api_key_enc"):
                try:
                    dealer_key = decrypt(r["torn_api_key_enc"])
                    break
                except Exception:
                    continue

        api_xanax, api_error = await verify_deposit(torn_id, week_start_ts, dealer_key)

        # The API total is this runner's WHOLE week — subtract what their other
        # submissions in the same week already claim, so reverifying one
        # submission can't double-count a deposit another submission already has.
        if api_xanax:
            already_claimed = 0
            for i, other in enumerate(runner["submissions"]):
                if i == self.sub_index or other.get("status") == "rejected":
                    continue
                # Judge the week by when the Xanax was DEPOSITED (from the pasted log),
                # not when it was submitted — so a late submission can't claim this week.
                dep = __import__("re").search(r'(\d{2}:\d{2}:\d{2}) - (\d{2}/\d{2}/\d{2})', other.get("deposit_log") or "")
                try:
                    if dep:
                        other_dt = datetime.strptime(f"{dep.group(2)} {dep.group(1)}", "%d/%m/%y %H:%M:%S").replace(tzinfo=timezone.utc)
                    else:
                        other_dt = datetime.fromisoformat(other["submitted_at"]).replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if week_start_dt <= other_dt < week_end_dt:
                    already_claimed += other.get("xanax_deposited", 0)
            api_xanax = max(0, api_xanax - already_claimed)

        if api_error:
            api_status = f"API Error: {api_error} — using self-reported amount ({sub['xanax_deposited']})"
            sub["self_reported"] = True
        elif not api_xanax:
            api_status = f"Not found in armory log — using self-reported amount ({sub['xanax_deposited']})"
            sub["self_reported"] = True
        elif api_xanax >= 94:
            api_status = f"Verified — {api_xanax} Xanax deposited"
            sub["xanax_deposited"] = api_xanax
            sub["self_reported"] = False
        else:
            api_status = f"Partial — {api_xanax} found"
            sub["xanax_deposited"] = api_xanax
            sub["self_reported"] = False

        sub["api_status"] = api_status
        sub["log_mismatch"] = False
        save_data(data)

        await interaction.message.edit(
            embed=build_review_embed(self.runner_discord_id, self.sub_index),
            view=ReviewView(self.runner_discord_id, self.sub_index)
        )

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="rev_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, int(self.runner_discord_id))
        sub = runner["submissions"][self.sub_index]
        if sub["status"] != "pending":
            return await interaction.response.send_message("Already processed.", ephemeral=True)
        await interaction.response.send_modal(
            RejectModal(self.runner_discord_id, self.sub_index, interaction.message)
        )

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, custom_id="rev_back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_pending(), view=PendingView())


# ── ALL RUNNERS VIEW ───────────────────────────────────────────────────────
class AllRunnersView(discord.ui.View):
    def __init__(self, offset=0):
        super().__init__(timeout=None)
        self.offset = offset
        data = load_data()
        runners = [(k, v) for k, v in data["runners"].items() if not v.get("is_dealer") or v.get("is_runner")]

        if runners:
            options = []
            for discord_id, runner in runners[:25]:
                name = runner.get("torn_name") or "Unknown"
                cr = weeks_remaining(runner)
                access = "Active" if runner.get("armory_access") else "Inactive"
                options.append(discord.SelectOption(
                    label=name,
                    value=discord_id,
                    description=f"{cr} week(s) | {access}"
                ))
            select = discord.ui.Select(
                placeholder="⚙ Manage Runner...",
                options=options,
                custom_id="ar_select"
            )
            select.callback = self._select_callback
            self.add_item(select)

        prev = discord.ui.Button(label="< Prev Week", style=discord.ButtonStyle.secondary, custom_id="ar_prev", row=1)
        prev.callback = self._prev_callback
        next_ = discord.ui.Button(label="Next Week >", style=discord.ButtonStyle.secondary, custom_id="ar_next", row=1)
        next_.callback = self._next_callback
        back = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, custom_id="ar_back", row=1)
        back.callback = self._back_callback
        self.add_item(prev)
        self.add_item(next_)
        self.add_item(back)
        if runners:
            history = discord.ui.Select(
                placeholder="📜 Access history...",
                options=[discord.SelectOption(label=o.label, value=o.value, description=o.description) for o in options],
                custom_id="ar_history",
                row=2
            )
            history.callback = self._history_callback
            self.add_item(history)
  
    async def _select_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        discord_id = interaction.data["values"][0]
        data = load_data()
        runner = data["runners"].get(str(discord_id), {})
        name = runner.get("torn_name") or "Unknown"
        await interaction.response.send_modal(ManageCreditsModal(discord_id, name, interaction.message))
    async def _history_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        discord_id = interaction.data["values"][0]
        runner = load_data()["runners"].get(str(discord_id), {})
        name = runner.get("torn_name") or "Unknown"
        lines = []
        for e in runner.get("access_history", [])[-15:]:
            try:
                when = f"<t:{int(datetime.fromisoformat(e['time']).timestamp())}:f>"
            except Exception:
                when = e.get("time", "?")[:16]
            exp = (e.get("expires") or "—")[:10]
            state = "✅" if e.get("access") else "❌"
            detail = f" — {e['detail']}" if e.get("detail") else ""
            lines.append(f"{when} **{e.get('by', '?')}**: {e.get('action', '?')}{detail} → {state} until {exp}")
        embed = discord.Embed(
            title=f"Access history — {name}",
            description="\n".join(lines) if lines else "No changes recorded yet. Logging started Nov 21, 2026.",
            colour=COL_GOLD
        )
        embed.set_footer(text="Newest at the bottom · last 15 changes")
        await interaction.response.send_message(embed=embed, ephemeral=True)  
  
    async def _prev_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        new_offset = self.offset + 1
        await interaction.message.edit(embed=build_leader_embed_all(new_offset), view=AllRunnersView(new_offset))

    async def _next_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        new_offset = max(0, self.offset - 1)
        await interaction.message.edit(embed=build_leader_embed_all(new_offset), view=AllRunnersView(new_offset))

    async def _back_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))


class ManageCreditsModal(discord.ui.Modal, title="Manage Credits"):
    amount = discord.ui.TextInput(
        label="Credits to add/remove (e.g. +2 or -1)",
        placeholder="Use negative number to remove credits",
        required=True, min_length=1, max_length=5
    )
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        placeholder="e.g. bonus week, penalty",
        required=False, max_length=200
    )

    def __init__(self, discord_id: str, runner_name: str, message: discord.Message):
        super().__init__()
        self.discord_id = str(discord_id)
        self.runner_name = runner_name
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            delta = int(self.amount.value.strip().replace("+", ""))
        except ValueError:
            return await interaction.followup.send("Invalid amount. Use a number like +2 or -1.", ephemeral=True)

        data = load_data()
        runner = get_runner(data, int(self.discord_id))
        old_weeks = weeks_remaining(runner)

        now = datetime.now(timezone.utc)
        if delta > 0:
            extend_access_expiry(runner, delta)
            runner["armory_access"] = True
        elif delta < 0 and runner.get("access_expires"):
            curr_exp = datetime.fromisoformat(runner["access_expires"]).replace(tzinfo=timezone.utc)
            new_expiry = curr_exp + timedelta(days=7 * delta)
            if new_expiry <= now:
                runner["access_expires"] = None
                runner["armory_access"] = False
            else:
                runner["access_expires"] = new_expiry.isoformat()
        note_access(runner, interaction.user.display_name, f"credits {'+' if delta >= 0 else ''}{delta}", self.reason.value.strip() if self.reason.value else "")
        save_data(data)

        reason_text = f" Reason: {self.reason.value.strip()}" if self.reason.value else ""
        new_weeks = weeks_remaining(runner)
        sign = "+" if delta >= 0 else ""
        await interaction.followup.send(
            f"✅ **{self.runner_name}**: {old_weeks} → {new_weeks} weeks ({sign}{delta}).{reason_text}",
            ephemeral=True
        )
        await self.message.edit(embed=build_leader_embed_all(0), view=AllRunnersView(0))


# ── REIMBURSE LIST VIEW ────────────────────────────────────────────────────
class ReimburseListView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        pending = get_pending_reimbursements()
        for discord_id, sub_index, runner, sub in pending[:4]:
            name = runner.get("torn_name") or "Unknown"
            owed = sub.get("credits_earned", 0) * REIMBURSEMENT
            btn = discord.ui.Button(
                label=f"✅ {name} — {owed:,.0f}",
                style=discord.ButtonStyle.success,
                custom_id=f"rlist_{discord_id}_{sub_index}"
            )
            btn.callback = self._make_reimburse_callback(discord_id, sub_index, name)
            self.add_item(btn)

        back = discord.ui.Button(label="← Back", style=discord.ButtonStyle.secondary, custom_id="rlist_back", row=4)
        back.callback = self._back_callback
        self.add_item(back)

    def _make_reimburse_callback(self, discord_id, sub_index, name):
        async def callback(interaction: discord.Interaction):
            if not is_leader(interaction.user):
              return await interaction.response.send_message("Leadership only.", ephemeral=True)
            await interaction.response.defer()
            data = load_data()
            runner = data["runners"].get(str(discord_id))
            if not runner:
                return await interaction.followup.send("Runner not found.", ephemeral=True)
            sub = runner["submissions"][sub_index]
            sub["reimbursed"] = True
            sub["reimbursed_by"] = interaction.user.display_name
            sub["reimbursed_at"] = datetime.now(timezone.utc).isoformat()
            save_data(data)
            ticket_id = sub.get("sheet_ticket_id")
            if ticket_id is not None:
                try:
                    from sheets import update_paid_status_in_sheet
                    await update_paid_status_in_sheet(ticket_id)
                except Exception as e:
                    print(f"[SHEETS] update paid error: {type(e).__name__}: {e}", flush=True)

            runner_name = runner.get("torn_name", "Runner")
            await interaction.followup.send(f"✅ **{runner_name}** marked as reimbursed.", ephemeral=True)

            try:
                guild = await get_guild(interaction.client)
                member = await guild.fetch_member(int(discord_id))
                await member.send(
                    f"💰 **Your reimbursement of {sub.get('credits_earned', 0) * REIMBURSEMENT:,.0f} has been sent!**\n"
                    f"Approved by {interaction.user.display_name}."
                )
            except Exception as e:
                print(f"[DM ERROR] reimburse_list: {type(e).__name__}: {e}", flush=True)

            await interaction.message.edit(embed=build_leader_embed_reimburse(), view=ReimburseListView())
        return callback

    async def _back_callback(self, interaction: discord.Interaction):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))

  # ── LEADER VIEW ─────────────────────────────────────────────────────────────
class LeaderView(discord.ui.View):
    def __init__(self, tab="dashboard", offset=0):
        super().__init__(timeout=None)
        self.tab = tab
        self.offset = offset
        pa = count_pending_approval()
        pr = count_pending_reimburse()
        pac = count_pending_access()
        for item in self.children:
            if hasattr(item, "custom_id"):
                if item.custom_id == "ld_pending":
                    item.label = f"Pending Approval ({pa})" if pa else "Pending Approval"
                elif item.custom_id == "ld_reimburse":
                    item.label = f"Pending Reimburse ({pr})" if pr else "Pending Reimburse"
                elif item.custom_id == "ld_access_pending":
                    item.label = f"Pending Access ({pac})" if pac else "Pending Access"

    @discord.ui.button(label="🔄 Dashboard", style=discord.ButtonStyle.primary, custom_id="ld_dashboard", row=0)
    async def dashboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))

    @discord.ui.button(label="All Runners", style=discord.ButtonStyle.secondary, custom_id="ld_all", row=0)
    async def all_runners(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_all(0), view=AllRunnersView(0))

    @discord.ui.button(label="Pending Approval", style=discord.ButtonStyle.secondary, custom_id="ld_pending", row=0)
    async def pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_pending(), view=PendingView())

    @discord.ui.button(label="Pending Reimburse", style=discord.ButtonStyle.secondary, custom_id="ld_reimburse", row=1)
    async def reimburse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_reimburse(), view=ReimburseListView())

    @discord.ui.button(label="Pending Access", style=discord.ButtonStyle.secondary, custom_id="ld_access_pending", row=1)
    async def pending_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_access_pending(), view=PendingAccessView())

    @discord.ui.button(label="Armory Access", style=discord.ButtonStyle.secondary, custom_id="ld_access", row=1)
    async def access(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_access(), view=LeaderView("access"))

    @discord.ui.button(label="📊 Sheets", style=discord.ButtonStyle.secondary, custom_id="ld_sheets", row=2)
    async def export_sheets(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.send_message(
            "📊 [Open XR Tracker Sheet](https://docs.google.com/spreadsheets/d/12XDoMd1kwZki55cQCS0mf934v2Zv934hOZEpuxwQ8oU/edit?gid=2139100444#gid=2139100444)",
            ephemeral=True
        )

    @discord.ui.button(label="Live Feed", style=discord.ButtonStyle.secondary, custom_id="ld_livefeed", row=2)
    async def live_feed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        embed = await build_live_feed_embed()
        await interaction.message.edit(embed=embed, view=LiveFeedView())

    @discord.ui.button(label="Dev", style=discord.ButtonStyle.secondary, custom_id="ld_dev", row=2)
    async def dev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in TEST_IDS:
            return await interaction.response.send_message("Dev view is restricted.", ephemeral=True)
        await interaction.response.defer()
        try:
            dev_key = os.getenv("DEV_API_KEY")
            print(f"[DEV] key present: {bool(dev_key)}", flush=True)
            if not dev_key:
                return await interaction.followup.send("DEV_API_KEY not set in environment.", ephemeral=True)
            embed = await build_dev_embed(dev_key)
            await interaction.message.edit(embed=embed, view=DevView())
        except Exception as e:
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"Dev error: {e}", ephemeral=True)

    @discord.ui.button(label="Role Check", style=discord.ButtonStyle.secondary, custom_id="ld_role_check", row=2)
    async def role_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_role_check_embed(), view=RoleCheckView())
  
    @discord.ui.button(label="< Prev Week", style=discord.ButtonStyle.secondary, custom_id="ld_prev", row=3)
    async def prev_week(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        new_offset = self.offset + 1
        await interaction.message.edit(embed=build_leader_embed_all(new_offset), view=LeaderView("all", new_offset))

    @discord.ui.button(label="Next Week >", style=discord.ButtonStyle.secondary, custom_id="ld_next", row=3)
    async def next_week(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        new_offset = max(0, self.offset - 1)
        await interaction.message.edit(embed=build_leader_embed_all(new_offset), view=LeaderView("all", new_offset))

    @discord.ui.button(label="👤 Toggle Runner Status", style=discord.ButtonStyle.secondary, custom_id="ld_toggle_runner", row=3)
    async def toggle_runner(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        data = load_data()
        runner = get_runner(data, interaction.user.id)
        is_currently_runner = runner.get("is_runner", False)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Toggle Runner Status",
                description=(
                    "**Disable Runner Status**\nYour submissions will be hidden from the dashboard and you will no longer be tracked as a runner. Existing data is preserved."
                    if is_currently_runner else
                    "**Enable Runner Status**\nYour Xanax submissions will appear in the dealer dashboard and be tracked like any other runner. You will receive reimbursement and armory access credits as normal."
                ),
                colour=0xFFA726
            ),
            view=ToggleRunnerConfirmView(interaction.user.id, not is_currently_runner),
            ephemeral=True
        )

    async def on_timeout(self):
        pass


# ── TOGGLE RUNNER CONFIRM VIEW ─────────────────────────────────────────────
class ToggleRunnerConfirmView(discord.ui.View):
    def __init__(self, discord_id: int, enable: bool):
        super().__init__(timeout=60)
        self.discord_id = discord_id
        self.enable = enable

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, custom_id="toggle_runner_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            return await interaction.response.send_message("Not your button.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        data = load_data()
        runner = get_runner(data, self.discord_id)
        runner["is_runner"] = self.enable
        save_data(data)
        status = "enabled" if self.enable else "disabled"
        await interaction.followup.send(
            f"✅ Runner status **{status}**. {'You are now tracked as a runner.' if self.enable else 'You are no longer tracked as a runner.'}",
            ephemeral=True
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="toggle_runner_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)


# ── LIVE FEED VIEW ──────────────────────────────────────────────────────────
class LiveFeedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary, custom_id="lf_refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        embed = await build_live_feed_embed()
        await interaction.message.edit(embed=embed, view=LiveFeedView())

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, custom_id="lf_back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))


# ── DEV VIEW ────────────────────────────────────────────────────────────────
class DevView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary, custom_id="dev_refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in TEST_IDS:
            return await interaction.response.send_message("Dev view is restricted.", ephemeral=True)
        await interaction.response.defer()
        dev_key = os.getenv("DEV_API_KEY")
        embed = await build_dev_embed(dev_key) if dev_key else discord.Embed(title="DEV_API_KEY not set", colour=COL_RED)
        await interaction.message.edit(embed=embed, view=DevView())

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, custom_id="dev_back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))



class RoleCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Recheck Now", style=discord.ButtonStyle.primary, custom_id="rc_recheck")
    async def recheck(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        import tasks as task_module          # local import — avoids circular import
        from data import load_settings
        before = load_settings().get("last_role_check")
        try:
            await task_module.check_faction_roles()   # same logic/alerts as the 15-min loop
        except Exception as e:
            print(f"[ROLE CHECK] manual recheck error: {type(e).__name__}: {e}", flush=True)
        after = load_settings().get("last_role_check")
        await interaction.message.edit(embed=build_role_check_embed(), view=RoleCheckView())
        if after == before:
            await interaction.followup.send("⚠️ Recheck failed (Torn API unavailable) — showing last results.", ephemeral=True)

    @discord.ui.button(label="← Dashboard", style=discord.ButtonStyle.secondary, custom_id="rc_back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_leader(interaction.user):
            return await interaction.response.send_message("Leadership only.", ephemeral=True)
        await interaction.response.defer()
        await interaction.message.edit(embed=build_leader_embed_dashboard(), view=LeaderView("dashboard"))
