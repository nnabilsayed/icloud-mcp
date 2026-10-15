# How Apple Reminders stores repeat, early reminders and Urgent (CloudKit)

Notes from reverse-engineering the iCloud Reminders CloudKit zone through
[pyicloud](https://github.com/timlaing/pyicloud), by decoding records that the
Reminders app itself wrote and then writing equivalent records back. Everything
below was observed on a single account (iPhone, iOS 26.x, Reminders upgraded to
the post-iOS 13 format). Treat it as observations, not a spec.

> This is an unofficial, private API. Apple can change it at any time.

## What was verified, and how

| Feature | Evidence |
|---|---|
| Repeat frequency is zero-based on the wire | Read rules the app created: "monthly" is stored as `Frequency=2`, and a "every 6 months" rule is `Frequency=2, Interval=6`. |
| Early reminder lives in `DueDateDeltaAlertsData` | Set one in the app, read the raw record: it appeared as a base64 JSON field and **no** `Alarm`/`AlarmTrigger` record was created. |
| Early reminder units 0 = minutes, 1 = hours, 2 = days | Created "1 day before" (`unit 2, count -1`), "3 hours before" (`unit 1, count -3`) and "15 minutes before" (`unit 0, count -15`) in the app and decoded all three. |
| Urgent = asset + encrypted checksum | Turned Urgent on in the app: two new fields appeared. Wrote the same shape from Python; the reminder fired as an Urgent alarm on the phone (confirmed by the author). |

Verified individually against app-made records: all four repeat values
(`0` daily, `1` weekly, `2` monthly, `3` yearly; each created in the app and
read back raw, then shown correctly by iCloud.com for rules written from
Python), hours (`1`) and days (`2`). They also match
[timlaing/pyicloud#402](https://github.com/timlaing/pyicloud/pull/402).

An all-day due date is stored as midnight UTC of that date with no `TimeZone`
field. Writing local midnight instead lands on the previous day east of UTC.

## 1. Repeat (RecurrenceRule) numbering is zero-based

`RecurrenceRule.Frequency` on the wire:

| Value | Meaning |
|---|---|
| 0 | daily |
| 1 | weekly |
| 2 | monthly |
| 3 | yearly |

pyicloud's `RecurrenceFrequency` enum is one-based (`DAILY=1 ... YEARLY=4`) and
passes `int(enum)` straight through, so a "weekly" rule written through the
library is stored as `2` and shows up as **monthly** in the app. Reading has the
mirror problem. The enum also has no member for `0`, so a daily rule cannot be
created through it directly; create with any member, then patch `Frequency` to
`0` with a record update.

## 2. Early reminder is a field, not an alarm record

The "Early Reminder" setting is stored on the `Reminder` record in
`DueDateDeltaAlertsData` (wire type `ENCRYPTED_BYTES`; the value is base64 of
UTF-8 JSON, not actually opaque):

```json
{
  "reminderIdentifier": "<reminder uuid>",
  "dueDateDeltaAlerts": [
    {
      "dueDateDeltaCount": -3,
      "dueDateDeltaUnit": 1,
      "identifier": "<uuid>",
      "creationDate": 813646978.64,
      "minimumSupportedAppVersion": 0
    }
  ],
  "accountIdentifier": "<uuid, per account>",
  "minimumSupportedVersion": 20230430
}
```

- `dueDateDeltaCount` is negative (before the due date).
- `dueDateDeltaUnit`: `0` = minutes (decoded for "15 minutes before"; written from Python as 5, 30
  and a custom 45, and the app showed each correctly), `1` = hours, `2` = days.
  Weeks and months (the app offers "1 week before" and "1 month before") were not decoded.
- An extra *time alarm* is not a substitute for an early reminder: an alarm at a time other than
  the due time makes the app show that time in the Time field (a 20:45 alarm on a 21:00 reminder
  showed "8:45 PM"), and the Early Reminder row stays "None".
- `creationDate` is seconds since the Apple reference date (2001-01-01).
- `accountIdentifier` is not exposed on the list or reminder record. It is
  only visible inside this field once the app has written one, so it has to be
  learned from an existing early reminder.
- To write it, send `{"type": "ENCRYPTED_BYTES", "value": "<base64 json>"}`
  together with a `ResolutionTokenMap` naming `dueDateDeltaAlertsData` and
  `lastModifiedDate`.

## 3. Urgent (iOS 26.2 alarm) is an asset plus an encrypted checksum

When Urgent is switched on, the reminder gains two fields (they are absent
otherwise):

- `UrgentPresentationAlarmsAsData`: an `ASSETID`. The downloaded file is a small
  **unencrypted** JSON document:

  ```json
  {"minimumSupportedVersion":20251103,
   "account":[{"isEnabled":true,"personID":"<base64, per account>","modifiedOn":813647742.72}]}
  ```

- `UrgentPresentationAlarmsChecksum`: a 128-hex-character string. In the schema
  it is `ENCRYPTED_STRING`; on the wire it is `{"type": "STRING", "isEncrypted": true}`.
  Sending it with type `STRING` and no `isEncrypted`, or labelled
  `ENCRYPTED_STRING`, is rejected with `BAD_REQUEST ... defined to be ENCRYPTED_STRING`.

### Writing it

1. Request an upload token:
   `POST /database/1/com.apple.reminders/production/private/assets/upload` with
   `{"tokens": [{"recordType": "Reminder", "fieldName": "UrgentPresentationAlarmsAsData", "recordName": "Reminder/<id>"}], "zoneID": {...}}`.
2. `POST` the JSON bytes (`Content-Type: application/octet-stream`) to the
   returned `url`. The response contains `singleFile` with `fileChecksum`,
   `receipt`, `referenceChecksum`, `size` and `wrappingKey`.
3. Update the reminder with `UrgentPresentationAlarmsAsData` set to
   `{"type": "ASSETID", "value": <singleFile>}` and the checksum field above, plus
   a `ResolutionTokenMap` naming `urgentPresentationAlarmsChecksum` and
   `lastModifiedDate`.

`personID` is per account and must be learned from a reminder where Urgent was
set in the app. The reminder needs a due date and time.

### Open question: the checksum

The checksum is **not** a plain SHA-512 of the asset bytes: that does not match
for the Urgent file, nor for the equivalent list-membership checksum
(`MembershipsOfRemindersInSectionsChecksum`). Roughly 400 simple constructions
(hashes and HMACs over the file, ids and keys) also did not match. Sending
`sha512(file).hexdigest()` was accepted and the alarm fired, so it may simply
not be validated server-side or at fire time. Nobody here has confirmed which.
If you know how Apple computes it, please say so.

## Other things worth knowing

- **Alarm records are for absolute-time and location triggers.** The zone also
  contains `Alarm` and `AlarmTrigger` records (date triggers carry base64
  `DateComponentsData`); early reminders and Urgent do not use them.
- **Editing preserves these fields.** An ordinary update through pyicloud only
  sends the fields it names, so Urgent and early reminders survived a title
  and notes edit unchanged (checked by comparing the raw fields before and after).
- **Connection flakiness.** Intermittent "Remote end closed connection" errors
  and DNS failures against `p*-ckdatabasews.icloud.com` occurred; retrying once
  is enough.

## Time alarms are an Alarm + AlarmTrigger pair tied to the due date

An app-made timed reminder has an `Alarm` record (`AlarmUID`, `Reminder`, `TriggerID`,
`DueDateResolutionTokenAsNonce`) and an `AlarmTrigger` record (`Type=Date`,
`DateComponentsData`, `Alarm`). `DateComponentsData` is `BYTES`: base64 of compact JSON
with the wall-clock parts and zone, e.g.
`{"hour":13,"timeZone":{"identifier":"Africa\/Cairo"},"year":2026,"era":1,"day":31,"month":10,"minute":0,"second":0}`.

`DueDateResolutionTokenAsNonce` ties the alarm to one version of the due date: across 8
app-made alarms it equals `counter * 1e11 + modificationTime` of the reminder's
`dueDate` entry in `ResolutionTokenMap`. pyicloud's `add_location_trigger` rewrites the
whole token map with only the fields it touched, which drops the `dueDate` entry, so
this project merges into the existing map instead.

Apple can also leave a reminder pointing at an alarm record that no longer exists
(`NOT_FOUND`); `alarms_for` raises for such reminders, so alarms are read raw and
dangling ids are skipped.

**Verified on a phone (iOS 26):** an alarm record at the due time notifies at that time; an
alarm at a different time notifies at its own time (and the app then shows that time in the
reminder's Time field); a reminder with a due time but **no alarm record shows the time but
never notifies**. A reminder made through the API with a due time therefore needs an
alarm at that time, which this project now creates by default (`notify=False` opts out).
The app has no separate setting for several time alarms: its Time field and the alarm are
the same thing, so extra alarms behave as additional notifications only.

## Daily repeat, subtasks and moving between lists

- **Daily repeat:** a rule created with `Frequency=0` in a single step shows as **Every Day**
  on a phone. (An earlier create-as-1-then-patch-to-0 approach was the one that looked wrong
  on a phone; rules are now created with the wire value directly and edited in place.)
- **Subtask:** a reminder whose `ParentReminder` field references another reminder. The
  app shows it indented under the parent, and the parent's Subtasks count follows. The
  app nests one level.
- **Move:** the library's `update()` doesn't write the list. Writing the `List` reference
  plus a `ResolutionTokenMap` that keeps the existing entries and bumps `list` and
  `lastModifiedDate` produces a record identical (same fields, same token-map keys) to one
  the app writes when you move a reminder.
- **`update()` and alarms:** `update()` rewrites the whole `ResolutionTokenMap`, including
  the due-date token an alarm is tied to, so alarms are re-tied after every edit.

## iCloud read-after-write quirks seen while building undo

- A reminder read straight after a write can come back with an out-of-date change tag, so
  the next write is rejected with `CONFLICT (client oplock error updating record)`. The
  rejected write changes nothing, so saves retry with a fresh tag.
- Once, restoring a reminder's title, tags and list in quick succession left the title as a
  merge of the old and new text ("B origED") and a tag duplicated. It did not reproduce
  reliably, so undo now re-reads and compares, retries, and reports what still differs.
- Moving an event's start without giving an end must move the end too. Keeping the old end
  puts it before the new start and iCloud answers `403 Forbidden`.

## Lists: create, rename, delete

A list is one `List` record. Created, renamed and deleted in the Reminders app and read raw:

- **A new list:** `Name` (encrypted string), `SortingStyle=manual`, `IsLinkedToAccount=1`,
  `IsGroup=0`, `Imported=0`, `Deleted=0`, `ReminderIDs="[]"`, `ShouldCategorizeGroceryItems=0` and
  `ShouldAutoCategorizeItems=0` (both encrypted), and a `ResolutionTokenMap` with the keys
  `minimumSupportedVersion, name, parentAccount, reminderIDsMergeableOrdering,
  shouldAutoCategorizeItems, shouldCategorizeGroceryItems, sortingStyle`. No colour or icon fields
  when you pick the defaults. The app also edits the account's `ListIDsMergeableOrdering`
  (an opaque CRDT blob of list ids), but a list written without touching it still shows up on a
  phone, at the bottom of the sidebar.
- **Rename:** only the encrypted `Name` changes, and the `name` token goes counter + 1, new time,
  same replica. The account record is untouched.
- **Delete:** `Deleted=1`, `IsLinkedToAccount=0` and the `parentAccount` token bumped. The app
  also edits the account's list-order blob; leaving it alone works (the blob already keeps ids
  of deleted lists). A list deleted through this project matches one deleted in the app in
  every field except its name.

## Finding one event by uid on iCloud

iCloud's CalDAV server does not support searching by uid: the library's `get_event_by_uid`
sends a REPORT that iCloud answers with `412 Precondition Failed`. An event can instead be
fetched from its own address, `<calendar url>/<uid>.ics`, which holds for events made by this
project and (all 30 checked) by the Calendar app. A scan of the calendar is the fallback.

## Moving events between calendars, and what CalDAV does not expose

- **iCloud holds one uid in one calendar at a time.** Saving an event into a second calendar
  while the original still exists answers `412 Precondition Failed`, so copy-then-delete cannot
  work. WebDAV `MOVE` (source URL, `Destination` header, `Overwrite: F`) answers `201`, is atomic
  and keeps the uid and every detail.
- **URL, categories, status and priority** are stored and returned unchanged by iCloud.
- **Calendar properties over CalDAV:** a PROPFIND returns the colour (`calendar-color`, as
  `#RRGGBBAA`), the component set and the current user's privileges. For this account no calendar
  carried a subscription marker (`subscribed` resource type or `source`): holiday, birthday and sports
  subscriptions are not exposed over CalDAV. There is no property for a calendar's default alert.

## Apple Notes: a note that survives needs its CRDT structure

Notes the app writes carry more than text. Creating one through the CloudKit API with only the
text and formatting runs is accepted (`200`, the record exists, the library reads it back), and
then it is moved to Recently Deleted about a minute later. Three things were tried first and none
of them was the cause: the gzip header, the formatting runs (a copy of the app's own run, byte for
byte, still vanished), and the record-level fields. The cause is in the body.

`TextDataEncrypted` is base64 of a gzipped protobuf: `Document { 2: note }`, where the note holds

| field | what | for a note written in one insertion |
|---|---|---|
| 2 | the text (UTF-8) | |
| 3 (x3) | substrings: `{1: {replica, clock}, 2: length, 3: {replica, clock}, 5: index}` | root `(0,0) len 0`, then `(1,0) len N`, then an end sentinel `(0, 0xFFFFFFFF) len 0` |
| 4 | version vector: `{1: {1: replica uuid, 2: {1: clock}, 2: {1: 1}}}` | clock = `N`, then `1` |
| 5 (x runs) | formatting runs: `{1: length, 2: paragraph style, 13: timestamp}` | |

- `N` is the text length in **UTF-16 code units** (an emoji counts 2; Arabic letters count 1).
- The library's schema does not declare fields 3 and 4, so building a note through it drops
  them. That is why editing a copy of an app-made note works (the unknown fields ride along, as long
  as the length is unchanged) while every note built from scratch is trashed.
- The replica UUID must be one that the note's two blobs know (`ReplicaIDToNotesVersionDataEncrypted`
  and `ReplicaIDToUserIDEncrypted`). Copying the blobs and the replica from any note the app wrote
  works, and `notes.py` learns them once from the most recent notes and caches them.
- **A title is an explicit `style_type = 0`.** The app writes `{1: 0, 9: paragraph uuid}` for a
  title paragraph and omits field 1 for body text. A protobuf binding that drops default values
  cannot write that zero, so the runs are encoded by hand.
- Moving a note to Recently Deleted is a parent and folder-reference change (`TrashFolder-CloudKit`),
  not `Deleted = 1`; moving it back restores it.

Verified on one account (iPhone, iOS 26.x): five notes covering plain, pasted-style, title style,
a 301-unit body (multi-byte lengths) and Arabic plus an emoji all stayed in Notes for the full
two minutes, round-tripped exactly, and survived delete, undo and move. The body is checked
byte for byte against a note the app wrote in `tests/test_notes.py`.
