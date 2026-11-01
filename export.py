# ------------------------
# EXPORT HELPERS
# ------------------------

import discord
from discord.ext import commands
import io
import openpyxl
from db import get_all_users, get_pool
from helpers import get_org_role_ids, member_has_org_role
from logger import logger

# ------------------------
# EVENT EXPORT
# ------------------------
async def build_event_xlsx(bot: commands.Bot, event_id: int, guild: discord.Guild) -> tuple[io.BytesIO, str, int]:
    """
    Build an xlsx for a single saved event.
    Returns (buffer, event_name, attendee_count).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        event_row = await conn.fetchrow("SELECT name, created_at, dkp_awarded, kind FROM events WHERE id = $1", event_id)
        attendees = await conn.fetch("SELECT user_id FROM attendance WHERE event_id = $1", event_id)

    wb = openpyxl.Workbook()
    ws = wb.active
    if not ws:
        raise ValueError("Workbook has no active sheet.")

    ws.title = "Attendance"
    ws.append(["#", "Display Name", "UserID", "Member / Community Member", "DKP Awarded"])

    if event_row["kind"] == "attendance":
        dkp_val = "N/A (attendance)"
    else:
        dkp_val = event_row["dkp_awarded"] if event_row["dkp_awarded"] is not None else "N/A"
    org_role_ids = await get_org_role_ids()

    for i, row in enumerate(attendees, 1):
        uid = row["user_id"]
        member = guild.get_member(uid)
        if member:
            name = member.display_name
        else:
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except (discord.NotFound, discord.HTTPException):
                name = f"Unknown ({uid})"
        # Checked at export time: someone who has since left the org shows as a Community Member
        tag = "Member" if member and member_has_org_role(member, org_role_ids) else "Community Member"
        ws.append([i, name, uid, tag, dkp_val])

    # Summary sheet
    ws2 = wb.create_sheet("Summary")
    ws2.append(["Event ID", event_id])
    ws2.append(["Event Name", event_row["name"]])
    ws2.append(["Event Type", "Attendance only" if event_row["kind"] == "attendance" else "DKP"])
    ws2.append(["Created At", str(event_row["created_at"])])
    ws2.append(["DKP Awarded", dkp_val])
    ws2.append(["Attendee Count", len(attendees)])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer, event_row["name"], len(attendees)

# ------------------------
# DKP LEADERBOARD
# ------------------------

async def build_dkp_xlsx(bot: commands.Bot, guild: discord.Guild):
    """
    Build xlsx for the full DKP leaderboard.
    """
    users = await get_all_users()

    wb = openpyxl.Workbook()
    ws = wb.active
    if not ws:
        raise ValueError("Workbook has no active sheet.")
    
    ws.append(["Rank", "User", "UserID", "DKP"])

    for i, row in enumerate(users, 1):
        uid, dkp = row["user_id"], row["dkp"]
        member = guild.get_member(uid)
        if member:
            name = member.display_name
        else:
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except discord.NotFound:
                logger.warning("Could not fetch user %s during export", uid)
                name = "Unknown"
            except discord.HTTPException as e:
                logger.error("Failed to fetch user %s during export: %s", uid, e)
                name = "Unknown"
        ws.append([i, name, str(uid), dkp])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return buffer, users
# ------------------------
# ADJUSTMENT LIST
# ------------------------

def _when(ts) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC") if ts else ""

def build_adjustment_xlsx(guild: discord.Guild, adj_list: dict, lines: list[dict]) -> io.BytesIO:
    """Every line of an adjustment list, removed ones included, with who added it and what happened to it."""
    def name(uid):
        if not uid:
            return ""
        member = guild.get_member(uid)
        return member.display_name if member else f"Unknown ({uid})"

    wb = openpyxl.Workbook()
    ws = wb.active
    if not ws:
        raise ValueError("Workbook has no active sheet.")
    ws.title = "Lines"
    ws.append(["Line", "Member", "UserID", "Points", "Reason", "Added By", "Added At", "Status", "Removed By"])
    for line in lines:
        if line["removed_at"]:
            status = "Removed"
        else:
            status = {"applied": "Applied", "skipped": "Skipped"}.get(line["outcome"] or "", "Pending")
        ws.append([line["line_no"], name(line["user_id"]), str(line["user_id"]), line["amount"], line["reason"] or "",
                   name(line["added_by_id"]), _when(line["added_at"]), status, name(line["removed_by_id"])])

    active = [line for line in lines if not line["removed_at"]]
    ws2 = wb.create_sheet("Summary")
    ws2.append(["List ID", adj_list["id"]])
    ws2.append(["Name", adj_list["name"]])
    ws2.append(["State", adj_list["state"].capitalize()])
    ws2.append(["Created By", name(adj_list["created_by_id"])])
    ws2.append(["Created At", _when(adj_list["created_at"])])
    ws2.append(["Closed By", name(adj_list["closed_by_id"])])
    ws2.append(["Closed At", _when(adj_list["closed_at"])])
    ws2.append(["Lines", len(active)])
    ws2.append(["Members", len({line["user_id"] for line in active})])
    ws2.append(["Points Added", sum(line["amount"] for line in active if line["amount"] > 0)])
    ws2.append(["Points Removed", -sum(line["amount"] for line in active if line["amount"] < 0)])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
