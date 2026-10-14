"""One-time capture of per-account identifiers that Apple only exposes inside
records the Reminders app wrote itself.

Usage:
    uv run python capture.py <reminder-id-with-early-reminder> [<reminder-id-with-urgent>]

Find ids with the `list_reminders` tool. Writes .account_id / .urgent_person_id
next to this file (both are git-ignored).
"""
import sys

import reminders

if len(sys.argv) < 2:
    raise SystemExit(__doc__)

svc = reminders._service()
reminders.capture_account_id(svc, sys.argv[1])
print("Saved account id (early reminders enabled).")
if len(sys.argv) > 2:
    reminders.capture_urgent_person_id(svc, sys.argv[2])
    print("Saved Urgent person id (urgent enabled).")
