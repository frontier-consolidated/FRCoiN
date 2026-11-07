# FRCoiN Quick Guide — for officers
The everyday jobs in a few clicks. Everything happens in **#frcoin-panel** (officers and admins only). Replies to your clicks are private — only you see them. For the full walkthrough with screenshots, see the **Officer's Guide** in this forum.

## Run a DKP event
1. Click **+ Create Event**. Give it a name and pick the **voice scope**: a whole category (e.g. Events) catches every channel in it, even ones added later. Leave it blank to use the channel *you're* in.
2. When people are in voice, click **Snapshot** on the event's card. You don't need to be in voice yourself.
3. During the event, click **Update snapshot** as often as you like. It only *adds* people — nobody is removed automatically.
4. Fix the list with **Add member** / **Remove member**. Check it with **Attendance**.
5. Click **End event** and enter the points (e.g. `10`). Nothing is paid yet — use **Edit points** or **Reopen** if needed.
6. Click **Close event**. Points go to everyone still an org member, the leaderboard updates and the audit log records it.

Only org members can earn DKP. Anyone else in voice is skipped.

## Take attendance (no points)
Click **Take Attendance** instead of Create Event. It records **everyone** in voice — members and Community Members — and awards nothing. Snapshot as normal, then **Close event**.

## Other card buttons
• **Edit scope** — change which channels the event looks at. People already on the list stay on it.
• **Export list** — the attendance list as an Excel file.
• **Cancel** — throws the event away, no points. It asks you to confirm.

## Lost a card?
Several events can be open at once, and any officer can run any card. Pick the event in the lobby's **Open events** dropdown and its card jumps to the bottom of the channel. Cards keep working for hours and through bot restarts.
=== split ===
## Points outside events
Click **Adjustments** in the lobby (or type `/adjustment_mode`).
• **Spend** — a member cashes in points. Enter the amount, or leave it blank to reset them to 0. A reason is required.
• **Add / Remove points** — a one-off correction, with a reason. Points can only be *added* to org members.

## Adjustment lists — many changes at once
For an Arms Fayre, bank trades or a group bonus:
1. **Adjustments → New adjustment list**, give it a name (members see it in their history).
2. **Add lines**: pick up to 25 members, the points (`-40` to deduct, `5` to add) and an optional reason. Repeat as needed — any officer can add lines.
3. The card warns you if anyone can't cover their lines. **Remove lines** fixes mistakes (use the #numbers on the card).
4. **Apply** — only the officer who made the list, or an admin. Everyone is adjusted at once; anyone short is skipped and named. The audit log gets the full list as an Excel file.

## Quick fixes
• **"You don't have permission"** — your role isn't set as officer. Ask an admin.
• **Someone wasn't captured** — check they have the org role and were in one of the event's channels, then **Add member**.
• **"The bot can't see …"** — the bot needs View Channel on that voice channel. Ask an admin, or pick another channel.
• **Wrong points** — before closing: **Edit points**. After closing: **Adjustments → Add / Remove points** for each member.
• **A private panel "has expired"** — just open it again from the lobby.

Members check their own balance with the **My Points** button under the leaderboard.
