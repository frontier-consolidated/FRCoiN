import discord

EVENTS_PER_PAGE = 8

class EventsBrowserView(discord.ui.View):
    """Paginated view of all saved events for the guild, with export and delete buttons."""

    def __init__(self, events: list, guild: discord.Guild, invoker: discord.Member):
        super().__init__(timeout=120)
        self.events = events          # list of asyncpg Records
        self.guild = guild
        self.invoker = invoker
        self.page = 0
        self.total_pages = max(1, (len(events) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
        self._update_buttons()

    def _current_page_events(self):
        start = self.page * EVENTS_PER_PAGE
        return self.events[start:start + EVENTS_PER_PAGE]

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Saved Events",
            colour=discord.Colour.teal(),
            description="Select an event ID with `/event_export <id>` or `/event_delete <id>`."
        )
        page_events = self._current_page_events()
        for ev in page_events:
            status = "Ended" if ev["ended"] else "Active / Cancelled"
            dkp_str = f"{ev['dkp_awarded']} DKP" if ev["dkp_awarded"] is not None else "—"
            ts = ev["created_at"].strftime("%Y-%m-%d %H:%M UTC") if ev["created_at"] else "Unknown"
            embed.add_field(
                name=f"`ID {ev['id']}` — {ev['name']}",
                value=f"{status} • Awarded: {dkp_str} • {ts}",
                inline=False
            )
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages} • {len(self.events)} total events • FRCoiN")
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
