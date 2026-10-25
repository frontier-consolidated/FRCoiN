import discord

DKP_LOG_PER_PAGE = 10

class DkpLogBrowserView(discord.ui.View):
    """Paginated view of a member's DKP transaction history."""

    def __init__(self, entries: list, target: discord.Member, invoker: discord.Member):
        super().__init__(timeout=120)
        self.entries = entries        # list of asyncpg Records
        self.target = target
        self.invoker = invoker
        self.page = 0
        self.total_pages = max(1, (len(entries) + DKP_LOG_PER_PAGE - 1) // DKP_LOG_PER_PAGE)
        self._update_buttons()

    def _current_page_entries(self):
        start = self.page * DKP_LOG_PER_PAGE
        return self.entries[start:start + DKP_LOG_PER_PAGE]

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f"DKP Log — {self.target.display_name}", colour=discord.Colour.blurple())

        lines = []
        for row in self._current_page_entries():
            amount = row["amount"]
            sign = "+" if amount >= 0 else ""
            reason = row["reason"] or "—"
            issued_by = f"<@{row['issued_by_id']}>" if row["issued_by_id"] else "system"
            ts = row["created_at"].strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"`{sign}{amount}` • {reason} • by {issued_by} • {ts}")
        embed.description = "\n".join(lines)

        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages} • {len(self.entries)} total entries • FRCoiN")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message("Only the person who ran this command can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
