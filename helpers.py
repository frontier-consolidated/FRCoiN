# ------------------------
# HELPERS
# ------------------------

import discord
from db import get_config

async def is_admin(member: discord.Member) -> bool:
    roles = await get_config("admin_roles")
    if not roles:
        return False
    return any(str(r.id) in roles.split(",") for r in member.roles)

async def is_officer(member: discord.Member) -> bool:
    if await is_admin(member):
        return True
    roles = await get_config("officer_roles")
    if not roles:
        return False
    return any(str(r.id) in roles.split(",") for r in member.roles)

# ------------------------
# ORG ROLE FILTERING
# ------------------------
def parse_role_ids(raw: str) -> list[int]:
    """Parse a comma-separated string of role mentions or IDs into a list of ints."""
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.startswith("<@&") and part.endswith(">"):
            part = part[3:-1]
        if part.isdigit():
            ids.append(int(part))
    return ids

async def get_org_role_ids() -> list[int]:
    """Return the configured org role IDs, or [] if not set."""
    raw = await get_config("org_roles")
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]

def member_has_org_role(member: discord.Member, org_role_ids: list[int]) -> bool:
    """Return True if the member holds at least one org role."""
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(org_role_ids))

# ------------------------
# SLASH COMMAND ACCESS (rollout phase 2 switch)
# ------------------------
async def slash_allowed(member: discord.Member) -> bool:
    """Officer-tier slash commands: officers by default, admins only once `slash_access` is 'admin'. Buttons ignore this."""
    if await get_config("slash_access") == "admin":
        return await is_admin(member)
    return await is_officer(member)

# ------------------------
# VOICE SCOPE
# ------------------------
VOICE_TYPES = (discord.VoiceChannel, discord.StageChannel)

def scope_voice_channels(guild: discord.Guild, scope_channel_ids: list[int] | None) -> list[discord.VoiceChannel | discord.StageChannel]:
    """
    The voice/stage channels an event's scope covers. Each stored id is a voice channel or a category;
    a category covers every voice/stage channel under it, read live so channels created mid-event are included.
    Deleted channels are skipped, and a channel picked both directly and via its category is counted once.
    """
    channels: dict[int, discord.VoiceChannel | discord.StageChannel] = {}
    for channel_id in scope_channel_ids or []:
        scope = guild.get_channel(channel_id)
        if isinstance(scope, discord.CategoryChannel):
            channels.update((c.id, c) for c in scope.channels if isinstance(c, VOICE_TYPES))
        elif isinstance(scope, VOICE_TYPES):
            channels[scope.id] = scope
    return list(channels.values())

def collect_scope_members(
    guild: discord.Guild, scope_channel_ids: list[int] | None, fallback_member: discord.Member | None
) -> tuple[list[discord.Member], dict[str, int]] | None:
    """
    Everyone (bots excluded) in the event's voice scope, plus a {channel name: count} breakdown.
    With no stored scope, falls back to the clicking officer's current voice channel (1.x behaviour).
    Returns None if there's nothing to snapshot (scope deleted, or no scope and the officer isn't in voice).
    """
    if scope_channel_ids:
        channels = scope_voice_channels(guild, scope_channel_ids)
        if not channels and not any(guild.get_channel(i) for i in scope_channel_ids):
            return None
    elif fallback_member and fallback_member.voice and isinstance(fallback_member.voice.channel, VOICE_TYPES):
        channels = [fallback_member.voice.channel]
    else:
        return None

    members: list[discord.Member] = []
    breakdown: dict[str, int] = {}
    for channel in channels:
        humans = [m for m in channel.members if not m.bot]
        breakdown[channel.name] = len(humans)
        members.extend(humans)
    return members, breakdown
