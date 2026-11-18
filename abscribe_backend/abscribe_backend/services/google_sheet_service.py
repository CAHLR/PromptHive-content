import os

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.service_account import Credentials as service_account_Credentials

from abscribe_backend.services.latex_converter_service import \
    annotate_sheet_data

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
# SPREADSHEET_ID = "19k_-uEjf-i_r2jvibSV8hOPE99hdE1yOSFQr7CsEANg"

# Resolve credential locations as absolute paths so they work regardless of the
# process' current working directory. Each can be overridden via env vars.
_SERVICES_DIR = os.path.dirname(__file__)
_PACKAGE_DIR = os.path.dirname(_SERVICES_DIR)          # .../abscribe_backend/abscribe_backend
_BACKEND_ROOT = os.path.dirname(_PACKAGE_DIR)          # .../abscribe_backend

SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    os.path.join(_SERVICES_DIR, "service_account_file.json"),
)
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", os.path.join(_BACKEND_ROOT, "token.json"))
CREDENTIALS_FILE = os.environ.get(
    "GOOGLE_OAUTH_CLIENT_FILE", os.path.join(_PACKAGE_DIR, "credentials.json")
)

# Optional API key for reading PUBLIC ("anyone with the link can view") sheets
# without OAuth. This sidesteps the OAuth consent-screen / verification wall and
# is sufficient for the read-only difficulty-filter testing flow.
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")


class GoogleAuthError(RuntimeError):
    """Raised when usable Google credentials cannot be obtained.

    Carries a human-readable, actionable message that is safe to surface to the
    frontend (it does not leak secrets).
    """


def _load_service_account_credentials():
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        return None
    try:
        return service_account_Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    except Exception as e:  # pragma: no cover - defensive
        print(f"Service account credentials could not be loaded: {e}")
        return None


def get_credentials(spreadsheet_id=None):
    # 1) Prefer a service account if one is configured.
    service_account_credentials = _load_service_account_credentials()
    if service_account_credentials is not None:
        return service_account_credentials

    # 2) Fall back to a stored user OAuth token.
    credentials = None
    if os.path.exists(TOKEN_FILE):
        credentials = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            with open(TOKEN_FILE, "w") as token:
                token.write(credentials.to_json())
            return credentials
        except RefreshError as e:
            # The stored refresh token is expired or revoked (invalid_grant).
            # Drop it so a fresh interactive re-auth can run, and raise a clear,
            # non-500 error that the API layer can return to the UI.
            try:
                os.remove(TOKEN_FILE)
            except OSError:
                pass
            raise GoogleAuthError(
                "Google authorization has expired (invalid_grant). Restore access by "
                "running: python -m abscribe_backend.reauth_google"
            ) from e

    # 3) No usable token. Only launch the interactive browser flow when explicitly
    #    enabled (avoids blocking normal API requests on a browser popup).
    if (
        os.environ.get("GOOGLE_ALLOW_INTERACTIVE_AUTH", "0") == "1"
        and os.path.exists(CREDENTIALS_FILE)
    ):
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        credentials = flow.run_local_server(port=62161)
        with open(TOKEN_FILE, "w") as token:
            token.write(credentials.to_json())
        return credentials

    raise GoogleAuthError(
        "No valid Google credentials available. To read a PUBLIC sheet without "
        "OAuth, set GOOGLE_API_KEY. Otherwise provide a service account file at "
        f"{SERVICE_ACCOUNT_FILE} or run: python -m abscribe_backend.reauth_google"
    )


def _sheets_service(spreadsheet_id=None):
    """Build a Sheets API client for READ operations.

    Preference order:
      1. Service account (if configured) — works for private sheets shared with it.
      2. API key (GOOGLE_API_KEY) — works for public sheets, no OAuth required.
      3. Stored user OAuth token — falls back to the interactive flow / errors.
    """
    if not os.path.exists(SERVICE_ACCOUNT_FILE) and GOOGLE_API_KEY:
        return build("sheets", "v4", developerKey=GOOGLE_API_KEY)
    credentials = get_credentials(spreadsheet_id)
    return build("sheets", "v4", credentials=credentials)

def get_sheet_data(spreadsheet_id, range_, filter_criteria=None):
    try:
        service = _sheets_service(spreadsheet_id)
        sheets = service.spreadsheets()

        if filter_criteria:
            filter_string = ""
            for column, criteria in filter_criteria.items():
                filter_string += f"{column}='{criteria}' AND "
            filter_string = filter_string[:-5]  # Remove the last 'AND'
            range_ += f"&q={filter_string}"

        result = sheets.values().get(spreadsheetId=spreadsheet_id, range=range_).execute()
        data = result.get("values", [])
        annotate_sheet_data(sheet=data)
        return data
    except HttpError as error:
        return {"error": str(error)}
    except GoogleAuthError as error:
        return {"error": str(error)}
    except Exception as error:  # pragma: no cover - defensive
        print(f"get_sheet_data failed: {error}")
        return {"error": str(error)}

def get_sheet_titles(spreadsheet_id):
    try:
        service = _sheets_service(spreadsheet_id)
        sheets = service.spreadsheets()
        spreadsheet = sheets.get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title))").execute()
        sheets_list = spreadsheet.get("sheets", [])
        sheet_titles = [sheet["properties"]["title"] for sheet in sheets_list]
        return sheet_titles
    except HttpError as error:
        return {"error": str(error)}
    except GoogleAuthError as error:
        return {"error": str(error)}
    except Exception as error:  # pragma: no cover - defensive
        print(f"get_sheet_titles failed: {error}")
        return {"error": str(error)}

def get_file_name(spreadsheet_id):
    try:
        service = _sheets_service(spreadsheet_id)
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        file_name = spreadsheet.get("properties", {}).get("title", "")
        return file_name
    except HttpError as error:
        return {"error": str(error)}
    except GoogleAuthError as error:
        return {"error": str(error)}
    except Exception as error:  # pragma: no cover - defensive
        print(f"get_file_name failed: {error}")
        return {"error": str(error)}
    


def append_rows(spreadsheet_id, range_, values):
    credentials = get_credentials(spreadsheet_id)
    try:
        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets().values()

        body = {
            "values": values
        }
        result = sheets.append(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()
        return result
    except HttpError as error:
        return {"error": str(error)}

def create_new_sheet(spreadsheet_id, sheet_title):
    credentials = get_credentials(spreadsheet_id)
    try:
        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets()

        add_sheet_request = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": sheet_title
                        }
                    }
                }
            ]
        }

        result = sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=add_sheet_request
        ).execute()

        return result
    except HttpError as error:
        return {"error": str(error)}

def delete_rows(spreadsheet_id, sheet_name, start_index, end_index):
    credentials = get_credentials(spreadsheet_id)

    try:
        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets()

        delete_request = {
            "requests": [
                {
                    "deleteRange": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": start_index,
                            "endRowIndex": end_index
                        },
                        "shiftDimension": "ROWS"
                    }
                }
            ]
        }

        result = sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=delete_request
        ).execute()

        return result
    except HttpError as error:
        return {"error": str(error)}
    
def update_columns(spreadsheet_id, range_, values):
    credentials = get_credentials(spreadsheet_id)
    try:
        service = build("sheets", "v4", credentials=credentials)
        sheets = service.spreadsheets().values()

        body = {
            "values": values
        }
        result = sheets.update(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()

        return result
    except HttpError as error:
        return {"error": str(error)}
    
def get_sheet_id(spreadsheet_id, sheet_name):
    credentials = get_credentials(spreadsheet_id)
    try:

        service = build("sheets", "v4", credentials=credentials)
        spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

        sheets = spreadsheet.get('sheets', '')
        for sheet in sheets:
            if sheet['properties']['title'] == sheet_name:
                return sheet['properties']['sheetId']
                

        return None  # Return None if the sheet name doesn't match any sheets
    except HttpError as error:
        return {"error": str(error)}
    

