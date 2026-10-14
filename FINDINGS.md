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
| Early reminder units 1 = hours, 2 = days | Created "1 day before" (`unit 2, count -1`) and "3 hours before" (`unit 1, count -3`) in the app and decoded both. |
| Urgent = asset + encrypted checksum | Turned Urgent on in the app: two new fields appeared. Wrote the same shape from Python; the reminder fired as an Urgent alarm on the phone (confirmed by the author). |

Verified individually against app-made records: monthly (`2`), hours (`1`),
days (`2`). The remaining repeat values (`0` daily, `1` weekly, `3` yearly)
follow from the zero-based scheme and match
[timlaing/pyicloud#402](https://github.com/timlaing/pyicloud/pull/402) but were
not each confirmed against an app-made rule.

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
- `dueDateDeltaUnit`: `1` = hours, `2` = days. Other units were not tested.
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

