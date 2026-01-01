"""
Google Drive & Google Sheets import helpers for AgencyVault.
"""

from typing import List, Dict
import io
import csv

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


def _get_credentials(service_account_info: dict) -> Credentials:
    return Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


def import_google_sheet(
    service_account_info: dict,
    spreadsheet_id: str,
    range_name: str,
) -> List[Dict[str, str]]:
    creds = _get_credentials(service_account_info)
    service = build("sheets", "v4", credentials=creds)

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_name)
        .execute()
    )

    values = result.get("values", [])
    if not values:
        return []

    headers = values[0]
    rows = values[1:]

    records = []
    for row in rows:
        record = {}
        for i, header in enumerate(headers):
            record[header] = row[i] if i < len(row) else ""
        records.append(record)

    return records


def import_drive_csv(
    service_account_info: dict,
    file_id: str,
) -> List[Dict[str, str]]:
    creds = _get_credentials(service_account_info)
    service = build("drive", "v3", credentials=creds)

    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()

    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    fh.seek(0)
    decoded = fh.read().decode("utf-8").splitlines()
    reader = csv.DictReader(decoded)

    return list(reader)

