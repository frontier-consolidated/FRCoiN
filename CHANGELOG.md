# Changelog

All notable changes to FRCoiN are documented here.

## 2.1.0 — adjustment lists (unreleased)

**DB schema:** changes, new tables only (`adjustment_lists`, `adjustment_lines`), created automatically on first start. Nothing existing is altered, so 2.0.0 still runs against a 2.1.0 database (rollback = redeploy 2.0.0). **Pre-upgrade export:** not needed.

### Added

- **Adjustment lists** — Adjustments → **New adjustment list** posts a card in the panel channel where officers collect many +/- point lines (e.g. an Arms Fayre or bank trades), each with an optional reason, then apply them all at once.
  - Lines are checked as they're added: additions are for org members only, and a deduction can't take a member below 0 (counting their other lines on the list). The card keeps a live balance check, refreshed whenever any balance changes.
  - Any officer can add or remove lines; only the officer who created the list, or an admin, can **Apply** or **Cancel** it (both confirm first).
  - Applying is one database transaction. Each member's lines go through together; a member who can no longer cover their deductions (or would gain points but has left the org) is skipped and named. A list can't be applied twice.
  - The audit log gets a summary plus an Excel file of every line — reason, who added it, when, and whether it was applied, skipped or removed. Members see each applied line in My Points as `adjlist:<id> <list name>: <reason>`.
  - Open lists appear in the lobby (`[ADJ]`) and `/status`, and their cards survive restarts like event cards.

## 2.0.0 — button panel (unreleased)

**DB schema:** changes, additive only. New columns on `events` and new config keys, applied automatically on first start. `users`, `dkp_log` and `attendance` are untouched, so balances and history carry over. 1.x still runs against a migrated DB (rollback = redeploy 1.x). **Pre-upgrade export:** optional — see `SETUP.md`.

### Added

- **Button panel** in one Officers+-only channel (`/setup panel_channel:#…`): a lobby message plus one live card per open event. The bot locks the channel to admin/officer roles (needs **Manage Roles**); `/status` warns if it isn't locked down.
- **Several events can be open at once.** Slash commands gain an optional `event` option with autocomplete; with one open event it's picked automatically.
- **Multi-channel snapshots.** An event's voice scope can be one or more voice channels, a whole category, or a mix — one click captures them all, the officer doesn't need to be in voice, and channels created mid-event in a chosen category are included. Two events can run in the same category by each picking their own channels; creating an event whose channels overlap another open event's shows a warning. The card shows a per-channel breakdown, who is new and who has left voice. Snapshots are additive.
- **Attendance-only events** (`/start_event type:Attendance only` or **Take Attendance**): capture everyone in scope, tagged Member / Community Member in the card and export. No DKP.
- **Points are awarded on Close.** On a card, End event stores pending points (Edit points / Reopen are available); Close awards them in one DB transaction, DMs the event's creator the summary and deletes the card. Double clicks can't award twice.
- `/adjustment_mode` (Spend, Add/Remove points) and `/admin_mode` (Setup pickers, Status, Saved events, exports, Delete event, Import leaderboard, Re-post panel, slash-command access toggle) — ephemeral panels, also on the lobby.
- **My Points** button under the leaderboard: your balance, rank, last 3 entries and full history, visible only to you.
- `slash_access` setting (`/setup` or the Admin panel): switch officer slash commands to admin-only once officers use the panel. Off by default.
- Event exports gain a Member / Community Member column and an Event Type row.

### Changed

- **Only org members can gain DKP.** DKP snapshots always capture org members only, Add member refuses non-members, Close skips anyone who lost their org role (listed in the summary), and positive adjustments to non-members are refused. Negative adjustments and `/spend` still work on anyone.
- `/snapshot_event` no longer has the `org_only` option — the event type decides who is captured. Use an attendance event to record Community Members.
- `/end_event` still awards immediately, and now also removes the event's card and DMs its creator.
- The panel and every card are restored on startup, so they keep working across restarts and deploys.
- Voice scopes are checked against what the **bot** can see (Discord's channel picker lists what the officer can see). Picking a channel or category the bot can't view is refused with its name; a category containing hidden voice channels gets a warning, and snapshots name any channel they couldn't check instead of counting it as empty. A snapshot for an event with no scope refuses when the officer's own voice channel is hidden from the bot, instead of capturing nobody.
- **Officer's Guide 2.0.0** (`Officer_Guide_2.0.0.docx` / `.pdf`) replaces `Officer_Guide.docx`: a panel-first walkthrough with screenshots of every screen officers see. The slash commands are kept as a reference section.

### Fixed

- `/spend` (and the panel's Spend button) now records the amount deducted in the DKP log, e.g. `-50`. It used to record the member's new balance, so a spend of 50 from 152 showed as `+102`. Entries written before 2.0.0 keep the old values, because the deducted amount wasn't stored.

## 2026-10-26

### Changed

- `/spend` now accepts the Officer tier, not just Admin.

## 2026-10-25

### Added

- New "officer" permission tier (`officer_roles`, configurable via `/setup`), with an `is_officer()` helper that lets officers run the day-to-day event/adjustment commands without full admin access.
- `/dkp_log` is now self-service: anyone can view their own DKP history (leave `member` blank); checking someone else's history still requires officer/admin or an org role. Results are paginated via a new `DkpLogBrowserView` instead of being truncated to the last 10 entries.
- `/import_leaderboard` now automatically backs up the current leaderboard to the audit channel before wiping and replacing it. If the backup can't be sent (e.g. no audit channel configured), the import is aborted instead of proceeding with no safety net.
- A global error handler (`bot.tree.error` and `PointsModal.on_error`) now catches unhandled command/modal exceptions, logs them, and replies with a friendly message instead of a silent "application did not respond" failure.
- Startup now fails fast with a clear message if `DISCORD_TOKEN` or `DATABASE_URL` is missing, instead of an opaque error deep in `discord.py`/`asyncpg`.
- `/setup` can now update a single option at a time — every parameter is optional, and only the options you pass are changed. Pass `none` for `org_roles`/`officer_roles` to explicitly clear them.

### Changed

- `/spend` now accepts an optional `amount` (deduct a specific amount, clamped at 0) and a required `reason`, instead of unconditionally resetting a member's DKP to 0 with a hardcoded reason.
- `/snapshot_event`'s `org_only` option now defaults to `True` (org members only) instead of `False`, so officers no longer need to remember to enable it for routine events. Pass `org_only:False` to capture everyone, including non-org guests.
- `/start_event`, `/snapshot_event`, `/end_event`, `/cancel_event`, `/event_add`, `/event_remove`, `/event_attendance`, `/events_saved`, `/event_export`, `/adjust`, `/dkp_log` (viewing others), and `/status` now accept the Officer tier, not just Admin.
- `get_dkp_log_entries`'s default fetch limit increased from 10 to 500 to support pagination.

### Fixed

- `/event_remove`'s audit embed logged the raw event database record instead of the event ID.

### Docs

- `README.md` gained a Features/Commands overview.
- `Officer_Guide.docx` corrected stale command names (`/event_list` → `/event_attendance`, `/export` → `/export_leaderboard`), added a Permission Tiers section and per-command tier annotations, documented the redesigned `/spend` and per-option `/setup`, added `/dkp`/`/dkp_log`/`/status` to the command reference, and updated the `/snapshot_event` walkthrough for the new `org_only` default.
- Removed all emoji from bot messages, embeds, and source comments.
