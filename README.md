# icloud-mcp

An MCP server that lets AI agents (Claude Code, Codex, and other MCP clients)
manage your iCloud **Calendar** and **Reminders** from Windows, with no Mac or
iPhone in the loop.

- **Calendar** uses CalDAV with an app-specific password. This is the
  supported route.
- **Reminders** uses iCloud's private CloudKit web API through
  [pyicloud](https://github.com/timlaing/pyicloud). It is unofficial. See
  [Caveats](#caveats).

Both go beyond the basics:

- **Calendar:** all-day and repeating events, alerts, and an `agenda` tool that
  returns today's (or this week's) events and due reminders in one call.
- **Reminders:** repeat, early reminders, tags and **Urgent** alarms. How those
  are stored is written up in [FINDINGS.md](FINDINGS.md).

## Tools

### Calendar

`list_calendars`, `list_events`, `create_event`, `update_event`, `delete_event`,
`create_calendar`, `rename_calendar`, `delete_calendar`,
`get_event`, `search_events`, `move_event`, `duplicate_event`,
`list_calendar_details`, `set_calendar_color`.

- **All-day events:** `start`/`end` as `YYYY-MM-DD` (`end` is the last day, inclusive; the
  default is a one-day event).
- **Repeat:** `repeat` (`daily|weekly|monthly|yearly`, `none` to remove), `repeat_interval`, and
  either `repeat_count` or `repeat_until`; weekly also takes `repeat_days` like `["MO","WE"]`.
  Editing or deleting a repeating event affects the whole series.
- **Alerts:** `alerts` is a list of minutes before the start (`[30, 1440]` = 30 minutes and
  1 day before); on update it replaces the existing alerts and `[]` clears them.
- **Extra fields:** `url`, `categories` (tags), `status` (`TENTATIVE`, `CONFIRMED`, `CANCELLED`) and
  `priority` (1 highest to 9, 0 none) on `create_event` / `update_event` (`''` / `[]` / `0` clears).
- **Move and copy:** `move_event(uid, to_calendar)` moves an event (and a repeating event's whole
  series) with the same uid; `duplicate_event(uid, to_calendar, start, end, title)` copies it under a
  new uid, optionally into another calendar or at a new time (a new `start` alone keeps its length).
- **Calendar settings:** `list_calendar_details()` lists every calendar's colour, kind, whether it
  is writable and whether it is a read-only subscription (iCloud may not expose subscriptions over
  CalDAV at all); `set_calendar_color(calendar, color)` changes the colour. A calendar's default
  alert can't be read or set over CalDAV (Apple keeps it as an app setting).
- **Find events:** `get_event(uid, calendar)` returns one event's full details by id (the
  calendar is optional). `search_events(text, calendar, start, end, include_past, limit)` finds
  events by words in the title, notes or location without knowing the date; by default it skips
  events that are already over and lists a repeating event once.
- **Moving an event** (a new `start` without an `end`) keeps its length.
- **Conflicts:** `create_event` reports `conflicts` for a timed event (it still creates it;
  `warn_conflicts=False` skips the check).
- Calls retry up to 3 times on dropped connections or DNS blips; a create never duplicates an
  event that was saved before the connection dropped.
- **Calendars:** `create_calendar(name, color)` (names unique; optional `#RRGGBB`),
  `rename_calendar(calendar, new_name)` and `delete_calendar(calendar, delete_events)`. A calendar
  that still holds events is refused unless `delete_events=True` (up to 500 events, kept in the
  log so undo can recreate the calendar and its events). Undoing a create only removes the
  calendar if it is still empty.

### Reminders

`list_reminder_lists`, `list_reminders`, `create_reminder`, `update_reminder`,
`complete_reminder`, `delete_reminder`, `add_subtask`, `bulk_reminders`, `create_list`,
`rename_list`, `delete_list`.

- **Fields:** `due` (`YYYY-MM-DD` for all-day, or an ISO datetime in local time), `notes`,
  `priority`, `flagged`, `repeat` (`daily|weekly|monthly|yearly` with `repeat_interval`,
  `repeat_count`; an existing rule is edited in place), `tags`, `early_hours` / `early_days`,
  and `urgent` (turn-on only).
- **Links:** `links` attaches URLs (on update: the complete set, `[]` removes all).
- **Alarms:** `alarms` are extra time alarms, each `YYYY-MM-DDTHH:MM` local or `due` for the due
  time (on update: the complete set, `[]` removes them; location alarms are never touched). A
  reminder created or moved to a due *time* gets an alarm at that time by default, because a
  due time with no alarm never notifies (`notify=False` keeps it silent).
- **Smart views:** `list_reminders(flagged=True, priority='high'|'medium'|'low'|'none'|'any')`,
  sorted most urgent first. Details report `links`, `alarms`, `subtasks` and `parent`.
- **Subtasks:** `add_subtask(parent_id, title, ...)` nests a reminder under another (same list,
  one level deep). `update_reminder(parent=<id>)` re-parents (`""` makes it top-level), and
  deleting a reminder deletes its subtasks too.
- **Move:** `update_reminder(move_to=<list>)` moves a reminder and its subtasks.
- **Bulk:** `bulk_reminders(action, ...)` runs `complete`, `uncomplete`, `flag`, `unflag`, `move`,
  `reschedule` (`shift_days` or `new_due`), `set_priority` or `delete` on many reminders. Pick
  them with `ids` or filters (`list`, `flagged`, `priority`, `overdue`, `due_from`/`due_to`,
  `title_contains`); at least one is required, max 50 per call. **It only previews unless you
  pass `apply=True`.**
- **Lists:** `create_list(name)`, `rename_list(list, new_name)` and `delete_list(list,
  delete_reminders)`. Names must be unique. A new list appears at the bottom of your lists;
  there is no colour or icon option yet (the app picks its default). `delete_list` refuses a list
  that still holds reminders unless `delete_reminders=True`.

### Both

- `agenda(start, days, include_overdue)`: events from every calendar, reminders due in the
  range, and overdue reminders in one call (default: today).
- `find_free_time(duration_minutes, start, days, work_start, work_end, weekdays,
  buffer_minutes, all_day_blocks, calendars, limit)`: the open stretches (at least
  `duration_minutes` long) inside working hours across all calendars.
- `check_conflicts(start, end)`: what overlaps a proposed time.

## Activity log and undo

Every change made through this server (creating, editing, completing or deleting events,
reminders, lists and calendars, including each item of a bulk operation) is written to a local log with the
time, the agent that did it, a plain summary and what's needed to undo it.

- `activity_log(limit, agent, tool, only_undoable)` shows it, newest first.
- `undo(entry_id)` reverses one change; `undo(batch=...)` reverses a whole bulk operation
  (newest first); `undo()` reverses the most recent undoable change. Calendar changes are
  restored exactly. Reminder edits restore title, notes, due, priority, flag, completion,
  repeat, tags, links, alarms, list and parent; early-reminder and Urgent settings are not
  restored. Undo re-reads the reminder to check it matches, retries, and says so if it can't.
- Changes made directly in the Calendar/Reminders apps are not recorded.
- The agent's name comes from the MCP client's name; set `ICLOUD_MCP_AGENT` as a fallback.
- The log is `.activity/activity.jsonl` next to the server (git-ignored, because it holds event
  and reminder titles); set `ICLOUD_MCP_DATA_DIR` to move it. Logging never makes a tool fail.

### Notes

`create_note`, `move_note`, `delete_note`, `list_note_folders`, `list_notes`, `read_note`.

- **Limited reading:** `list_note_folders()` and `list_notes(folder, limit)` give titles only;
  `read_note(id)` returns the plain text of one note (formatting, checklists and attachments are
  left out). Locked notes are refused, nothing searches note bodies, and note text is never
  written to the activity log. `create_note(text, folder)` makes a note whose first line is the title; `delete_note` moves it to Recently Deleted
  (kept 30 days), and `undo` reverses a create, move or delete.
- **One-time setup:** the first `create_note` learns your account's note author identity from a
  note the Notes app wrote; if you have none, create any note in the app first. It is cached in
  `.notes_identity` (git-ignored).
## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

1. **App-specific password** (Calendar): at appleid.apple.com go to
   Sign-In and Security, App-Specific Passwords, and create one.
2. **Environment variables:**
   ```powershell
   setx ICLOUD_USERNAME "you@icloud.com"
   setx ICLOUD_APP_PASSWORD "xxxx-xxxx-xxxx-xxxx"
   ```
3. **Reminders sign-in (once):** run this in your own terminal. It asks for your
   Apple ID password and a 2FA code, then caches a trusted session.
   ```powershell
   uv run python login.py
   ```
4. **Register with your client**, for example Claude Code:
   ```
   claude mcp add icloud -- uv run --directory "C:\path\to\icloud-mcp" python server.py
   ```
   Codex (`config.toml`):
   ```toml
   [mcp_servers.icloud]
   command = "uv"
   args = ["run", "--directory", "C:\\path\\to\\icloud-mcp", "python", "server.py"]
   ```
5. **One-time capture for early reminders and Urgent.** Apple does not expose
   two per-account identifiers anywhere except inside records the app wrote.
   Set an early reminder, and turn Urgent on, on any reminder in the Reminders
   app, then run `uv run python capture.py <early-reminder-id> [<urgent-reminder-id>]`.
   The ids are stored next to the code in `.account_id` and `.urgent_person_id`
   (both git-ignored).

## Caveats

- **Unofficial and fragile.** Reminders uses the same private endpoints as
  icloud.com. Apple can change them without notice, and this may stop working.
- **It signs in with your real Apple ID password and 2FA code**, unlike
  Calendar's revocable app-specific password. The cached session is a
  credential: keep it private. Review the code, and the pyicloud dependency,
  before trusting it with your account.
- **Terms of service.** Automated access to private iCloud endpoints may not be
  something Apple permits. Use at your own risk, on your own account.
- **Urgent is confirmed working** on the author's iPhone: the reminder fires as
  an Urgent alarm. Apple's checksum algorithm for the Urgent file is not
  reverse-engineered; a SHA-512 of the file is accepted, so it works, but the
  real algorithm is unknown. Only turning Urgent on is implemented.
- **Advanced Data Protection** can block web access to iCloud data. This was not
  tested with it on.
- Written and tested against one account, so treat the details in
  `FINDINGS.md` as observations.
- Not affiliated with or endorsed by Apple.

## License

See `LICENSE`.

## Tests

`uv run pytest` runs the offline suite (no iCloud account or network needed). It covers
all-day and repeating events, alerts, date-range matching, retries, the agenda tool,
and the Reminders repeat/due-date handling.
