# FRCoiN

Frontier Consolidated "DKP" Bot

## Setup

See **[SETUP.md](SETUP.md)** for running the bot, the Discord invite and permissions, first-run server configuration, and upgrading from 1.x.

**Officers:** see the **[Officer's Guide 2.1.0](Officer_Guide_2.1.0.pdf)** for how to use the panel.

Quick start: `python -m venv .venv`, `.venv/bin/pip install .`, copy `.env.example` to `.env` and fill it in, then `.venv/bin/python -m main` (on Windows use `.venv\Scripts\…`).

## The panel

Officers run everything from buttons in one **Officers+-only panel channel** (`/setup panel_channel:#…`). Regular members never see it — the bot locks the channel down for you.

- **Lobby** — *Create Event* (DKP), *Take Attendance* (no DKP), *Adjustments*, *Admin*, and a dropdown that brings any open event's card back to the bottom.
- **Event cards** — one per open event; any officer can operate any card: *Snapshot / Update snapshot*, *Add / Remove member*, *Attendance*, *Export list*, *End event → Close event*, *Cancel*. Cards keep working for hours and across bot restarts.
- **Multi-channel snapshots** — give an event one or more voice channels, or a whole category, as its scope and one click captures them all, without being in voice yourself. Two events can share a category by each picking their own channels; you're warned if their scopes overlap. The card shows the per-channel breakdown, who's new, and who has left voice.
- **Only org members can earn DKP.** Everyone else is a *Community Member*: recorded by attendance events, never awarded DKP.
- **Adjustment lists** — collect many +/- point lines (an Arms Fayre, bank trades) on one card, each with an optional reason, then apply them all at once. Only the list's creator or an admin can apply it, and everything is recorded in the audit log.
- **My Points** — a button under the leaderboard that shows anyone their own balance, rank and history, privately.

## Features

- Event-based DKP: start/snapshot/end/cancel events, with voice attendance capture across a whole category and manual add/remove. Several events can be open at once.
- DKP is awarded on Close, in one database transaction — double clicks can't award twice. The event's creator is DM'd the summary.
- Attendance-only events record everyone in voice, tagged Member / Community Member.
- Manual DKP adjustment and spend-down, both with a required reason — every change is written to the `dkp_log` table and the audit channel.
- Self-service balance and history via the leaderboard's **My Points** button, `/dkp` and `/dkp_log`.
- Full leaderboard export/import via `.xlsx`, with an automatic pre-import backup posted to the audit channel — the import aborts if the backup can't be sent.
- Two-tier permissions: **Admin** (`admin_roles`) and **Officer** (`officer_roles`, optional).
- A global error handler logs unhandled errors and replies with a friendly message instead of a silent failure.

## Commands

| Command | Tier | Notes |
|---|---|---|
| `/setup` | Admin | Config: leaderboard/audit/panel channels, admin/officer/org roles, slash-command access — only the options you pass are changed |
| `/adjustment_mode` | Officer+ | Opens the Adjustments panel (Spend, Add/Remove points) |
| `/admin_mode` | Admin | Opens the Admin panel (Setup pickers, Status, exports, imports, saved events) |
| `/start_event` | Officer+ | Optional `type` (DKP / Attendance only) and `scope` (category or voice channel; pick several via the panel's Create Event) |
| `/snapshot_event`, `/end_event`, `/cancel_event` | Officer+ | Event lifecycle. Optional `event` when several are open |
| `/event_add`, `/event_remove`, `/event_attendance` | Officer+ | Manual attendance editing. Optional `event` |
| `/events_saved`, `/event_export` | Officer+ | Browse/export past events |
| `/event_delete` | Admin | Permanently delete a saved event |
| `/adjust` | Officer+ | Manual DKP +/- with required reason (positive amounts: org members only) |
| `/spend` | Officer+ | Deduct DKP (optional amount, or reset to 0), required reason |
| `/export_leaderboard`, `/import_leaderboard` | Admin | Full leaderboard backup/restore (auto-backs-up before import) |
| `/dkp`, `/dkp_log` | Everyone (self) / Officer+ or org role (others) | Balance + paginated history |
| `/status` | Officer+ | Config, open events, and panel lockdown check |

"Officer+" means the command works for anyone holding an `officer_roles` role, or any admin. If `officer_roles` is left unset, only admins can run those commands.

Once officers are comfortable with the panel, admins can set `/setup slash_access:Admin-only` (or use the Admin panel toggle): officer-tier slash commands then become admin-only, officers get pointed to the panel, and members to **My Points**. It's instant and reversible.
