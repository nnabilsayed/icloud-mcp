# Roadmap

Everything we could build, in one place. Effort: **S** (an hour), **M** (a session),
**L** (several sessions). Risk: **low** = official or already verified here,
**med** = works in the library but needs a live check on a real device,
**high** = unofficial, fragile, or sensitive.

Legend: `[x]` done, `[ ]` not done. "Library" means
[pyicloud](https://github.com/timlaing/pyicloud) (the upstream project this builds
on); its method names below come from inspecting the installed version.

## 0. Done

- [x] Calendar: list calendars, list/create/update/delete events (CalDAV)
- [x] Calendar: all-day events (inclusive end date)
- [x] Calendar: repeating events (daily/weekly/monthly/yearly, interval, count/until, weekdays)
- [x] Calendar: alerts (minutes before start)
- [x] Calendar: retries on dropped connections and DNS blips, with no duplicate creates
- [x] Calendar: date-range matching that works for floating (zone-less) times
- [x] Calendar: legacy "Reminders" list hidden from calendars
- [x] Reminders: list lists, list/create/update/complete/delete reminders
- [x] Reminders: repeat (verified wire values 0-3), early reminders, tags, Urgent alarms
- [x] Reminders: all-day due dates stored the way the app stores them
- [x] `agenda`: events from all calendars + due + overdue reminders in one call
- [x] 41 offline tests; README, FINDINGS, public repo, About box and topics
- [x] Upstream report: timlaing/pyicloud#419 (repeat off-by-one, verified)

## 1. Reminders: in the library, not exposed yet

- [ ] **Location reminders** ("when I arrive/leave"): `add_location_trigger`. M, med
- [x] **URL attachments** on a reminder: `create_url_attachment`, `attachments_for`,
      `update_attachment`, `delete_attachment`. S-M, med
- [x] **Timed alarms** separate from the due date (read `alarms_for`; creating them
      needed reverse-engineering the app's Alarm + AlarmTrigger records). M, med
- [ ] **Rename or recolor a tag**: `update_hashtag`. S, low
- [x] **Edit an existing repeat rule in place** instead of delete + create:
      `update_recurrence_rule`. S, low
- [ ] **"What changed since X"** feed: `iter_changes` / `sync_cursor`. M, med
      (the basis for notifications and the activity log, section 11)
- [x] **Move a reminder to another list** (matches an app-made move record)
- [ ] **Search reminders by text** across all lists. S, low (client-side filter)
- [x] **Subtasks** (the library's `create` takes a parent; verified on a phone)
- [x] **Priority and flag smart views** ("everything flagged", "all high priority"). S, low
- [x] **Bulk operations**: complete / move / reschedule many at once, with a preview. M, low
- [x] **Create, rename, delete lists** (done by matching records the app wrote). NOTE: the library has no call for
      this, so it would mean reverse-engineering CloudKit writes the way we did for
      Urgent. Do not promise it.
- [ ] Recurring reminders: skip one occurrence / complete only this one. M, high

## 2. Calendar: standard CalDAV, not exposed yet

- [ ] **Edit or cancel a single occurrence** of a repeating event (RECURRENCE-ID /
      EXDATE). M-L, low (standard iCalendar, fiddly)
- [x] **Create, rename, delete calendars** (CalDAV `make_calendar`). S-M, med
- [ ] **Get one event by uid** (without scanning), and **search events by text**. S, low
- [ ] **Event URL, notes and extra fields** (URL, categories, status, priority). S, low
- [ ] **Time zone support**: accept an explicit zone, default to yours, stop creating
      floating times. M, med (changes behavior, so test carefully)
- [ ] **Travel time** / "time to leave" if iCloud stores it in a readable field. M, high
- [ ] **Attendees and invites**: library has an `invites` module; sending from outside
      Apple's apps is unreliable. M-L, high
- [ ] **Move an event between calendars**, **duplicate an event**. S, low
- [ ] **Colors and calendar settings** (color, default alert). S, med
- [ ] **Subscribed calendars** (holidays, sports) read-only listing. S, low
- [ ] **ICS import / export** of events. M, low
- [x] **Free/busy**: when are you busy, ignoring titles (via `find_free_time`). S, low

## 3. Scheduling helpers (built on what we already read)

- [x] **Free-time finder**: "find 45 minutes this week" across all calendars,
      with working hours and buffers. M, low  <- best next step
- [x] **Conflict check** before creating an event. S-M, low
- [ ] **Find a slot and book it** in one call. M, low
- [ ] **Smart reschedule**: "push everything on Thursday to Friday". M, low
- [ ] **Day/week summary stats**: hours booked, busiest day, empty days. S, low
- [ ] **Overdue triage**: walk through overdue items (do / move / drop). S-M, low
- [ ] **Natural-language capture** ("dentist next Tuesday 3pm, remind me the day
      before"). S-M, low (mostly a better tool description plus date parsing)

## 4. Notes (library: read-focused)

The service exposes `folders`, `get`, `in_folder`, `iter_all`, `recents`,
`export_note`, `render_note`, `download_attachment_to`, `stream_attachment`,
`iter_changes`. There is no create/update call, so Notes is likely **read-only**.

- [ ] Read-only spike: list folders, list notes, read one. S, high (verify first)
- [ ] Search notes by text; show recent notes. M, high
- [ ] Export a note to Markdown / HTML. S, med
- [ ] Download a note's attachments. S, med
- [ ] Writing notes. L, high. Not offered by the library; would need reverse-engineering.

## 5. iCloud Drive (library: `create_folders`, `get_file`, `send_file`, `rename_items`, ...)

- [ ] Browse folders and list files. S, med
- [ ] Download a file; upload a file. M, med
- [ ] Create folders, rename, move, move to trash. M, med
- [ ] Trash handling: list, recover, delete forever (guard this one). M, high
- [ ] App data (`get_app_data`). S, med

## 6. Contacts (library: `all`, `me`, read-only)

- [ ] List and search contacts; get one contact's details. S-M, med
- [ ] Your own card (`me`). S, low
- [ ] Add / edit / delete contacts via CardDAV (separate from the library). M-L, med
- [ ] Birthdays to calendar. M, med

## 7. Photos (library: `albums`, `upload`, `create_album`, `shared_streams`, `watch`)

Sensitive. Listed for completeness; recommended to skip or heavily restrict.

- [ ] List albums and counts. S, high
- [ ] Upload a photo; create an album. M, high
- [ ] Shared streams. M, high
- [ ] Download originals / thumbnails. M, high

## 8. Hide My Email (library: `generate`, `reserve`, `update_metadata`, `deactivate`, `delete`)

- [ ] List, generate and reserve a throwaway address with a label. S-M, med
- [ ] Deactivate / reactivate / delete addresses. S, med

## 9. Account and devices (library: `account`, `devices`, `family`, `storage`)

- [ ] Storage usage and plan summary. S, low
- [ ] Family members list. S, med
- [ ] Find My: list devices and battery. S, high
- [ ] Find My: location / play sound / lost mode. M, high. Strongly recommended NOT
      to give an agent this.

## 10. Other Apple surfaces we have not looked at

- [ ] Mail via IMAP/SMTP (official, app-specific password): read, search, send. L, med
- [ ] Email to calendar: "interview confirmed for the 18th at 1pm" becomes an event. L, med
- [ ] Shortcuts/automations hooks. M, high
- [ ] iCloud Keychain / passwords: never. (listed so nobody asks)

## 11. Agent-friendly features

- [x] **Activity log**: what each agent changed and when. M, low
- [x] **Undo** the last change (needs the log). M-L, med
- [x] **Dry-run / preview mode** for bulk changes with a confirm step. M, low
- [ ] **Per-agent permissions**: read-only vs read-write, per calendar or list. M, low
- [ ] **Tool descriptions tuned per client** (fewer round trips for Luna/Hermes). S, low
- [ ] **Structured error messages** the model can act on (not-found vs auth vs network). S, low
- [ ] **Pagination** for very long lists. S, low
- [ ] **Caching** list_events / agenda for a few seconds to save calls. S, low
- [ ] **Health check tool** that reports session state and calendar reachability. S, low
- [ ] **Session expiry notice** with the exact fix command in the error. S, low

## 12. Things that run on their own

- [ ] **Morning briefing** (events, due, overdue, conflicts) sent by Luna. M, low
- [ ] **Weekly review** every Sunday; flag empty and crowded days. M, low
- [ ] **Nudges** for reminders overdue more than N days. M, low
- [ ] **Event reminders to your phone** via a messaging channel the gateway has. M, med
- [ ] **Recurring chores generator** (create the next N occurrences by rule). S-M, low
- [ ] **Link reminders to GitHub/Shopify/Supabase** events (Hermes already has these). L, med

## 13. Reliability and quality

- [ ] Retries for **Reminders** beyond the single retry now there. S, low
- [ ] **Live smoke test script** (guarded, self-cleaning) run before releases. S-M, low
- [ ] CI: run the offline tests on every push. S, low
- [ ] Lint/format config and type hints. S, low
- [ ] A test that checks every tool's schema stays stable. S, low
- [ ] **Compatibility notes** per iOS / Windows / Python version. S, low
- [ ] Don't use deprecated caldav `name` attribute (use `get_display_name`). S, low
- [ ] Consistent datetime handling everywhere (one parse helper). S-M, low

## 14. Sharing the project

- [ ] **One-command install** and a setup guide (app-specific password, 2FA login,
      client config for Claude Code / Codex / Hermes). M, low
- [ ] **Release tags and a changelog.** S, low
- [ ] **Publish to PyPI** so `uvx icloud-mcp` works. M, low
- [ ] **A write-up of what we learned** (repeat numbering, all-day dates, floating
      times, Urgent alarms) for the pyicloud maintainers and others. S-M, low
- [ ] **Upstream PRs** to pyicloud: repeat mapping fix (offered on #419), all-day
      dates. M, low
- [ ] **Security notes**: never print credentials, rotation script, what each token can do. S, low

## 15. Known limits (not planned)

- Reminders and Notes rely on private, unofficial iCloud APIs; Apple can change them.
- The Reminders login session expires; re-login needs a 2FA code from you.
- A single occurrence of a repeating event cannot be edited yet (section 2).
- Reminder lists cannot be created or deleted through the library (section 1).

## Suggested order

1. Free-time finder + conflict check (section 3): no new APIs, high value.
2. Activity log + dry-run (section 11): makes multi-agent use safe.
3. Morning briefing (section 12): the most visible daily win.
4. Reminders: URL attachments, location, search, move (section 1), each checked on the phone.
5. Notes read-only spike (section 4): decides whether Notes is worth it.
6. Edit one occurrence (section 2).
7. Packaging and release (section 14).
