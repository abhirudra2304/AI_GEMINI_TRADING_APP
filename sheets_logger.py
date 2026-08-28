"""
sheets_logger.py

Drop this file next to your stock scanner script. It appends each day's
scan results (momentum, filings, BTST, etc.) as new rows to a shared
Google Sheet, so the full history accumulates day over day and Cowork
(or anything else) can read it later for trend analysis.

SETUP (one-time):
    1. Create a Google Cloud service account, enable Sheets API + Drive API,
       download its JSON key file.
    2. Create a Google Sheet, share it with the service account's email
       (Editor access).
    3. pip install gspread google-auth
    4. Fill in CREDENTIALS_PATH and SHEET_ID below (or pass them as
       arguments / env vars — see bottom of file).

USAGE (inside your scan script):

    from sheets_logger import log_scan_results

    results = [
        {"ticker": "RELIANCE", "category": "momentum", "price": 2950.5,
         "volume": 4500000, "score": 87.2, "notes": "5-day breakout"},
        {"ticker": "TATASTEEL", "category": "filing", "price": 165.3,
         "volume": 12000000, "score": 91.0, "notes": "Board approved buyback"},
        {"ticker": "INFY", "category": "btst", "price": 1810.0,
         "volume": 3000000, "score": 78.5, "notes": "Strong close, high volume"},
    ]
    log_scan_results(results)

Each dict becomes one row. Adjust the COLUMNS list below to match
whatever fields your scanner actually produces — you don't have to use
these exact field names, just keep the dict keys and COLUMNS in sync.
"""

import os
import datetime

import gspread
from google.oauth2.service_account import Credentials

# ---- CONFIGURE THESE ----------------------------------------------------

CREDENTIALS_PATH = os.environ.get("SCANNER_CREDS_PATH", "credentials.json")
SHEET_ID = os.environ.get("SCANNER_SHEET_ID", "1281qVbOO-EJhYBmlpzkvCHrNi9j2EQKK74ToZouBd5U")
WORKSHEET_NAME = "ScanData"  # tab name inside the sheet; created if missing

# Column order written to the sheet. Keep this in sync with the dict keys
# you pass into log_scan_results(). "date" and "scan_time" are added
# automatically — don't include them in your result dicts.
COLUMNS = ["date", "scan_time", "ticker", "category", "price", "volume", "score", "notes"]

# ---------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_worksheet():
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)

    try:
        worksheet = sheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=len(COLUMNS))
        worksheet.append_row(COLUMNS)  # header row

    return worksheet


def log_scan_results(results):
    """
    results: list of dicts, one per stock found in today's scan.
    Each dict should have keys matching COLUMNS (minus date/scan_time,
    which are filled in automatically).
    """
    if not results:
        print("No results to log.")
        return

    worksheet = _get_worksheet()

    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    rows = []
    for r in results:
        row = [date_str, time_str] + [r.get(col, "") for col in COLUMNS if col not in ("date", "scan_time")]
        rows.append(row)

    worksheet.append_rows(rows, value_input_option="USER_ENTERED")
    print(f"Logged {len(rows)} rows to '{WORKSHEET_NAME}' for {date_str}.")


if __name__ == "__main__":
    # Quick self-test — run `python sheets_logger.py` after setup to confirm
    # the connection works before wiring it into your real scanner.
    test_results = [
        {"ticker": "TEST", "category": "momentum", "price": 100.0,
         "volume": 123456, "score": 50.0, "notes": "connection test"},
    ]
    log_scan_results(test_results)