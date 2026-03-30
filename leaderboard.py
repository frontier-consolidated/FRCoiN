
# ------------------------
# LEADERBOARD
# ------------------------

import discord
from discord.ext import commands
from db import get_config, set_config, get_all_users
from logger import logger

async def update_leaderboard(bot: commands.Bot):
    channel_id = await get_config("leaderboard_channel")
    message_id = await get_config("leaderboard_message")

    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))
    if not channel:
        logger.warning("Leaderboard channel not found (id=%s).", channel_id)
        return
    
    if isinstance(channel, (discord.abc.PrivateChannel, discord.ForumChannel, discord.CategoryChannel)):
        logger.warning("Leaderboard channel is not a valid text channel (id=%s).", channel_id)
        return

    data = await get_all_users()
    embed = discord.Embed(title="🏆 FRCoiN DKP Leaderboard", colour=discord.Colour.gold())
    lines = []
    guild = channel.guild
    for i, row in enumerate(data[:50], 1):
        uid, dkp = row["user_id"], row["dkp"]
        member = guild.get_member(uid)
        if member:
            name = member.display_name
        else:
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except discord.NotFound:
                name = f"Unknown ({uid})"
            except discord.HTTPException as e:
                logger.warning("Could not fetch user %s: %s", uid, e)
                name = f"Unknown ({uid})"
        lines.append(f"`{i:>2}.` **{name}** — {dkp}")

    embed.description = "\n".join(lines) if lines else "*No entries yet.*"
    embed.set_footer(text=f"Showing top {min(len(data), 50)} of {len(data)} members • FRCoiN")

    if message_id:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(content=None, embed=embed)
            return
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning("Could not edit leaderboard message: %s", e)

    message = await channel.send(embed=embed)
    await set_config("leaderboard_message", message.id)
