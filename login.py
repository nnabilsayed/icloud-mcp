"""Interactive iCloud sign-in for the Reminders tools. Run in your own terminal:
    uv run python login.py
Prompts for Apple ID password and the 2FA code, then caches a trusted session.
"""
import getpass
import os

from pyicloud import PyiCloudService

apple_id = os.environ.get("ICLOUD_USERNAME") or input("Apple ID email: ").strip()
api = PyiCloudService(apple_id, getpass.getpass("Apple ID password (hidden): "))

if api.requires_2fa:
    if not api.validate_2fa_code(input("2FA code: ").strip()):
        raise SystemExit("2FA code rejected")
    if not api.is_trusted_session:
        api.trust_session()

print("Signed in. Reminders lists:", [l.title for l in api.reminders.lists()])
