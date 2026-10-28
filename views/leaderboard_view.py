import discord

from db import get_all_users, get_dkp_log_entries
from helpers import get_org_role_ids, member_has_org_role
from views.dkp_log_browser_view import DkpLogBrowserView

class LeaderboardView(discord.ui.View):
    """The leaderboard message's My Points button. Persistent: registered once with bot.add_view at startup."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="My Points", style=discord.ButtonStyle.primary, custom_id="frc:lb:mypoints")
    async def my_points_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if not interaction.guild_id or not isinstance(member, discord.Member):
            return
        org_role_ids = await get_org_role_ids()
        if not org_role_ids or not member_has_org_role(member, org_role_ids):
            return await interaction.response.send_message("DKP is only tracked for org members.", ephemeral=True)

        users = await get_all_users()
        rank = next((i for i, row in enumerate(users, 1) if row["user_id"] == member.id), None)
        balance = users[rank - 1]["dkp"] if rank else 0
        entries = await get_dkp_log_entries(interaction.guild_id, member.id)

        embed = discord.Embed(title=f"My Points — {member.display_name}", colour=discord.Colour.gold())
        embed.add_field(name="Balance", value=f"**{balance}** DKP", inline=True)
        embed.add_field(name="Rank", value=f"#{rank} of {len(users)}" if rank else "Unranked", inline=True)
        recent = [
            f"`{e['amount']:+}` • {e['reason'] or '—'} • {discord.utils.format_dt(e['created_at'], 'R')}"
            for e in entries[:3]
        ]
        embed.add_field(name="Recent", value="\n".join(recent) or "*No DKP transactions yet.*", inline=False)
        embed.set_footer(text="Only you can see this • FRCoiN")

        view = MyPointsView(entries, member) if entries else discord.utils.MISSING
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class MyPointsView(discord.ui.View):
    def __init__(self, entries: list, member: discord.Member):
        super().__init__(timeout=300)
        self.entries = entries
        self.member = member

    @discord.ui.button(label="My History", style=discord.ButtonStyle.secondary)
    async def history_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = DkpLogBrowserView(entries=self.entries, target=self.member, invoker=self.member)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)
