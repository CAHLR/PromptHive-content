"""Local check: can the Google service account read an OATutor sheet?

This does NOT touch the frontend or any server. It loads the service-account JSON
the backend expects, then tries to read the tab titles of a test sheet.

Run from the abscribe_backend directory:

    python test_service_account.py                 # uses the default test sheet
    python test_service_account.py <SPREADSHEET_ID> # test a specific sheet

Requires the google client libs (already in the backend deps):
    pip install google-api-python-client google-auth
"""

from __future__ import annotations

import os
import sys

# Default location the backend looks for the service account (overridable by env).
_SERVICES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "abscribe_backend", "services"
)
SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    os.path.join(_SERVICES_DIR, "service_account_file.json"),
)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# One of the OATutor test sheets from earlier testing.
DEFAULT_SHEET_ID = "1omcfK-sc_u_xL4lSJSHJNEmburzBcTRM_BcMoV_uBwI"


def main() -> int:
    sheet_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SHEET_ID

    print(f"Service account file: {SERVICE_ACCOUNT_FILE}")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print("\nERROR: service account file not found.")
        print("Place your (rotated) Google service-account JSON at the path above,")
        print("or set GOOGLE_SERVICE_ACCOUNT_FILE to its location, then re-run.")
        return 1

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        print(f"\nERROR: missing Google libraries: {exc}")
        print("Install with: pip install google-api-python-client google-auth")
        return 1

    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    except Exception as exc:
        print(f"\nERROR: could not load the service account JSON: {exc}")
        return 1

    print(f"Service account email: {creds.service_account_email}")
    print(f"Testing read access to sheet: {sheet_id}\n")

    try:
        service = build("sheets", "v4", credentials=creds)
        meta = (
            service.spreadsheets()
            .get(spreadsheetId=sheet_id, fields="properties(title),sheets(properties(title))")
            .execute()
        )
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", "?")
        print(f"FAILED (HTTP {status}).")
        if str(status) in ("403", "404"):
            print("Most likely cause: the sheet is NOT shared with the service account.")
            print(f"Fix: open the sheet -> Share -> add {creds.service_account_email} as Viewer.")
        else:
            print(f"Details: {exc}")
        return 1
    except Exception as exc:
        print(f"FAILED: {exc}")
        return 1

    title = meta.get("properties", {}).get("title", "(unknown)")
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
    print("SUCCESS - the service account can read this sheet.")
    print(f"  File name: {title}")
    print(f"  Tabs ({len(tabs)}): {tabs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
