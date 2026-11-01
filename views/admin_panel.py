from typing import Callable, Union, cast
import discord
from discord.ext import commands

import actions
import panel
from db import get_config
from events import get_completed_events, OPEN_STATES
from helpers import is_admin
from views.adjust_panel import ExpiringPanel
from views.common import ConfirmView, guild_member, reply, run_action
from views.events_browser_view import EventsBrowserView

class AdminPanel(ExpiringPanel):
    @classmethod
    async def send(cls, bot: commands.Bot, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not await is_admin(interaction.user):
            return await reply(interaction, actions.NO_PERMISSION)
        view = cls(bot, interaction.user)
        view.slash_btn.label = f"Slash commands: {'Admin-only' if await get_config('slash_access') == 'admin' else 'Officer+'}"
        embed = discord.Embed(
            title="Admin",
            description="**Setup** changes need the Discord Administrator permission. Red buttons ask for confirmation.",
            colour=discord.Colour.dark_red()
        )
        await reply(interaction, embed=embed, view=view)
        view.opened_with = interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if not await is_admin(cast(discord.Member, interaction.user)):  # re-checked: roles may have changed since the panel opened
            await interaction.response.send_message(actions.NO_PERMISSION, ephemeral=True)
            return False
        return True

    # Row 0 — setup
    @discord.ui.button(label="Setup: Channels", style=discord.ButtonStyle.primary, row=0)
    async def channels_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(await SetupChannelsModal.build(self.bot, interaction))

    @discord.ui.button(label="Setup: Roles", style=discord.ButtonStyle.primary, row=0)
    async def roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(await SetupRolesModal.build(self.bot, interaction))

    @discord.ui.button(label="Slash commands: Officer+", style=discord.ButtonStyle.secondary, row=0)
    async def slash_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        new_mode = "officer" if await get_config("slash_access") == "admin" else "admin"
        message = await run_action(interaction, lambda: actions.apply_setup(self.bot, guild, actor, {"slash_access": new_mode}))
        if message:
            button.label = f"Slash commands: {'Admin-only' if new_mode == 'admin' else 'Officer+'}"
            if self.opened_with:
                await self.opened_with.edit_original_response(view=self)
            await reply(interaction, message)

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, row=0)
    async def status_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = guild_member(interaction)
        if ctx:
            await reply(interaction, embed=await actions.status_embed(ctx[0]))

    # Row 1 — events & files
    @discord.ui.button(label="Saved events", style=discord.ButtonStyle.secondary, row=1)
    async def saved_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        events = await get_completed_events(guild.id)
        if not events:
            return await reply(interaction, "No events have been saved for this server yet.")
        view = EventsBrowserView(events=events, guild=guild, invoker=actor)
        await reply(interaction, embed=view.build_embed(), view=view)

    @discord.ui.button(label="Export event", style=discord.ButtonStyle.secondary, row=1)
    async def export_event_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx

        async def export(i: discord.Interaction, event_id: int):
            result = await run_action(i, lambda: actions.export_event(self.bot, guild, actor, event_id))
            if result:
                file, message = result
                await reply(i, message, file=file)

        events = await get_completed_events(guild.id)
        if not events:
            return await reply(interaction, "No events have been saved for this server yet.")
        await interaction.response.send_modal(EventPickModal("Export event", events, export))

    @discord.ui.button(label="Export leaderboard", style=discord.ButtonStyle.secondary, row=1)
    async def export_lb_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        file = await run_action(interaction, lambda: actions.export_leaderboard(self.bot, guild, actor))
        if file:
            await reply(interaction, file=file)

    @discord.ui.button(label="Re-post panel", style=discord.ButtonStyle.secondary, row=1)
    async def repost_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, _ = ctx

        async def repost():
            if not await panel.get_panel_channel(guild):
                raise actions.ActionError("No panel channel set — use **Setup: Channels** first.")
            await panel.move_panel(self.bot, guild, await get_config("panel_channel"))
            return "Panel re-posted: the lobby and every open card are at the bottom of the panel channel."

        message = await run_action(interaction, repost)
        if message:
            await reply(interaction, message)

    # Row 2 — destructive
    @discord.ui.button(label="Delete event", style=discord.ButtonStyle.danger, row=2)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        events = [e for e in await get_completed_events(guild.id) if e["state"] not in OPEN_STATES]
        if not events:
            return await reply(interaction, "There are no closed events to delete.")

        async def picked(i: discord.Interaction, event_id: int):
            async def confirmed(c: discord.Interaction):
                message = await run_action(c, lambda: actions.delete_saved_event(self.bot, guild, actor, event_id))
                if message:
                    await reply(c, message)
            await reply(i, f"Permanently delete event `{event_id}` and its attendance records? This can't be undone.",
                        view=ConfirmView(actor, confirmed, confirm_label="Yes, delete it"))

        await interaction.response.send_modal(EventPickModal("Delete event", events, picked))

    @discord.ui.button(label="Import leaderboard", style=discord.ButtonStyle.danger, row=2)
    async def import_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImportModal(self.bot))

# ------------------------
# MODALS
# ------------------------
class EventPickModal(discord.ui.Modal):
    """Pick one of the 25 most recent saved events, then hand it to `on_pick`."""

    def __init__(self, title: str, events: list, on_pick: Callable):
        super().__init__(title=title)
        self.on_pick = on_pick
        self.event_select = discord.ui.Select(options=[
            discord.SelectOption(
                label=f"{e['id']} — {e['name']}"[:100],
                value=str(e["id"]),
                description=f"{'Attendance' if e['kind'] == 'attendance' else 'DKP'} · "
                            f"{e['created_at'].strftime('%Y-%m-%d')}"
                            + (f" · {e['dkp_awarded']} DKP" if e["dkp_awarded"] is not None else ""),
            )
            for e in events[:25]
        ])
        self.add_item(discord.ui.Label(text="Event", description="The 25 most recent are listed. Older ones: use the slash command with an ID.",
                                       component=self.event_select))

    async def on_submit(self, interaction: discord.Interaction):
        await self.on_pick(interaction, int(self.event_select.values[0]))

class ImportModal(discord.ui.Modal, title="Import leaderboard"):
    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.file = discord.ui.FileUpload(max_values=1)
        self.add_item(discord.ui.Label(
            text="FRCoiN .xlsx export", description="This REPLACES every balance. A backup is posted to the audit channel first.",
            component=self.file,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        attachment = self.file.values[0]

        async def confirmed(i: discord.Interaction):
            async def do_import():
                return await actions.import_leaderboard(self.bot, guild, actor, attachment.filename, await attachment.read())
            message = await run_action(i, do_import)
            if message:
                await reply(i, message)

        await reply(interaction, f"Replace the whole leaderboard with **{attachment.filename}**?",
                    view=ConfirmView(actor, confirmed, confirm_label="Yes, import"))

def _current_ids(raw: Union[str, None]) -> list[int]:
    return [int(r) for r in raw.split(",") if r.strip().isdigit()] if raw else []

class SetupChannelsModal(discord.ui.Modal, title="Setup: Channels"):
    FIELDS = (
        ("leaderboard_channel", "Leaderboard channel", "Where the leaderboard message lives"),
        ("audit_channel", "Audit channel", "Every bot action is logged here"),
        ("panel_channel", "Panel channel", "Officers+ only — the bot locks it down for you"),
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.selects: dict[str, discord.ui.ChannelSelect] = {}

    @classmethod
    async def build(cls, bot: commands.Bot, interaction: discord.Interaction) -> "SetupChannelsModal":
        modal = cls(bot)
        guild = interaction.guild
        for key, text, description in cls.FIELDS:
            current = await get_config(key)
            channel = guild.get_channel(int(current)) if guild and current else None
            select = discord.ui.ChannelSelect(
                channel_types=[discord.ChannelType.text], required=False,
                placeholder="Pick a text channel", default_values=[channel] if channel else [],
            )
            modal.selects[key] = select
            modal.add_item(discord.ui.Label(text=text, description=description, component=select))
        return modal

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        changes = {}
        for key, select in self.selects.items():
            if select.values:  # blank = keep current
                channel = guild.get_channel(select.values[0].id)
                if channel and str(channel.id) != await get_config(key):
                    changes[key] = channel
        if not changes:
            return await reply(interaction, "Nothing changed.")
        message = await run_action(interaction, lambda: actions.apply_setup(self.bot, guild, actor, changes))
        if message:
            await reply(interaction, message)

class SetupRolesModal(discord.ui.Modal, title="Setup: Roles"):
    FIELDS = (
        ("admin_roles", "Admin roles", "Full bot admins. Can't be empty."),
        ("officer_roles", "Officer roles", "Run events and adjustments. Deselect all to disable."),
        ("org_roles", "Org roles", "Only these members can earn DKP. Everyone else is a Community Member."),
    )

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.selects: dict[str, discord.ui.RoleSelect] = {}

    @classmethod
    async def build(cls, bot: commands.Bot, interaction: discord.Interaction) -> "SetupRolesModal":
        modal = cls(bot)
        guild = interaction.guild
        for key, text, description in cls.FIELDS:
            current = [r for r in (guild.get_role(i) for i in _current_ids(await get_config(key)))] if guild else []
            select = discord.ui.RoleSelect(
                min_values=1 if key == "admin_roles" else 0, max_values=25, required=key == "admin_roles",
                placeholder="Pick roles", default_values=[r for r in current if r],
            )
            modal.selects[key] = select
            modal.add_item(discord.ui.Label(text=text, description=description, component=select))
        return modal

    async def on_submit(self, interaction: discord.Interaction):
        ctx = guild_member(interaction)
        if not ctx:
            return
        guild, actor = ctx
        changes = {}
        for key, select in self.selects.items():
            picked = sorted(r.id for r in select.values)
            if set(picked) != set(_current_ids(await get_config(key))):
                changes[key] = picked
        if not changes:
            return await reply(interaction, "Nothing changed.")
        message = await run_action(interaction, lambda: actions.apply_setup(self.bot, guild, actor, changes))
        if message:
            await reply(interaction, message)
