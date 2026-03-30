from typing import Union
import discord
from discord.ext import commands
from db import add_dkp
from audit_logs import audit_embed, send_audit
from events import active_events, get_event_attendees, end_event
from leaderboard import update_leaderboard

class PointsModal(discord.ui.Modal, title="Enter DKP Points"):
    points = discord.ui.TextInput(label="Points (+ or -)", placeholder="e.g. 5 or -3")

    def __init__(self, bot: commands.Bot, event_mode=True, target_user: Union[discord.Member, None] = None,
                 guild_id: Union[int, None] = None, issued_by: Union[discord.Member, None] = None):
        super().__init__()
        self.bot = bot
        self.event_mode = event_mode
        self.target_user = target_user
        self.guild_id = guild_id
        self.issued_by = issued_by

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(self.points.value)
        except ValueError:
            await interaction.response.send_message(
                "Invalid number — please enter a whole number like `5` or `-3`.", ephemeral=True
            )
            return

        guild_id = interaction.guild_id
        if not guild_id or not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        issued_by_id = interaction.user.id

        if self.event_mode:
            active_event = active_events.get(guild_id)
            if not active_event:
                await interaction.response.send_message("No active event.", ephemeral=True)
                return

            attendees = await get_event_attendees(active_event)

            if not attendees:
                await interaction.response.send_message(
                    "No attendees recorded for this event. Use `/snapshot_event` first.", ephemeral=True
                )
                return

            for row in attendees:
                await add_dkp(row["user_id"], value, guild_id=guild_id,
                               reason=f"event:{active_event}", issued_by_id=issued_by_id)

            # Mark the event as ended and record the DKP awarded
            await end_event(active_event, value)

            await update_leaderboard(self.bot)
            active_events.pop(guild_id, None)

            await interaction.response.send_message(
                f"✅ Awarded **{value} DKP** to **{len(attendees)} users**.", ephemeral=True
            )

            embed = audit_embed("📋 Event DKP Awarded", discord.Colour.green())
            embed.add_field(name="Event ID", value=str(active_event), inline=True)
            embed.add_field(name="Points", value=f"{value:+}", inline=True)
            embed.add_field(name="Recipients", value=str(len(attendees)), inline=True)
            embed.add_field(name="Issued By", value=interaction.user.mention, inline=False)
            await send_audit(interaction.guild, embed)

        elif self.target_user:
            await add_dkp(self.target_user.id, value, guild_id=guild_id,
                           reason="manual_adjust", issued_by_id=issued_by_id)
            await update_leaderboard(self.bot)
            await interaction.response.send_message(
                f"✅ Updated **{self.target_user.display_name}** by **{value:+} DKP**.", ephemeral=True
            )

            embed = audit_embed("✏️ DKP Manually Adjusted", discord.Colour.orange())
            embed.add_field(name="Member", value=self.target_user.mention, inline=True)
            embed.add_field(name="Adjustment", value=f"{value:+}", inline=True)
            embed.add_field(name="Issued By", value=interaction.user.mention, inline=False)
            await send_audit(interaction.guild, embed)
