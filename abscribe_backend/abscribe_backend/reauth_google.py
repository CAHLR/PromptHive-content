"""Restore Google Sheets access by performing a fresh OAuth consent flow.

Run this once whenever the backend reports
"Google authorization has expired (invalid_grant)". It opens a browser window,
lets you sign in with a Google account that can read the OATutor sheet, and
writes a new ``token.json`` next to the backend package.

Usage (from the abscribe_backend directory):

    python -m abscribe_backend.reauth_google

Requires ``credentials.json`` (the OAuth client) to be present. Set
``GOOGLE_OAUTH_CLIENT_FILE`` / ``GOOGLE_TOKEN_FILE`` to override locations.
"""

from __future__ import annotations

import os

from google_auth_oauthlib.flow import InstalledAppFlow

from abscribe_backend.services.google_sheet_service import (
    CREDENTIALS_FILE,
    SCOPES,
    TOKEN_FILE,
)


def main() -> int:
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"ERROR: OAuth client file not found at {CREDENTIALS_FILE}")
        print("Set GOOGLE_OAUTH_CLIENT_FILE or place credentials.json there.")
        return 1

    # Remove any stale/expired token so the flow issues a brand new one.
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
            print(f"Removed stale token: {TOKEN_FILE}")
        except OSError as exc:
            print(f"WARNING: could not remove {TOKEN_FILE}: {exc}")

    print("Opening browser for Google authorization...")
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    credentials = flow.run_local_server(port=62161)

    with open(TOKEN_FILE, "w") as token:
        token.write(credentials.to_json())

    print(f"Success. New credentials written to {TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
