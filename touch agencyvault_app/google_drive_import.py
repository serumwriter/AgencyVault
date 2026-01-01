from typing import List, Dict
import csv
import io

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]


def _creds(service_account_info: dict) -> Credentials:
    return Credentials.from_service_account_info(
        service_account_info,
        scopes=SCOPES,
    )


def import_google_sheet(
    service_account_info: dict,
    spreadsheet_id: str,
    range_name: str,
) -> List[Dict[str, str]]:
    creds = _creds(service_account_info)
    service = build("sheets", "v4", credentials=creds)

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_name)
        .execute()
    )

    rows = result.get("values", [])
    if not rows:
        return []

    headers = rows[0]
    data = rows[1:]

    output = []
    for row in data:
        record = {}
        for i, h in enumerate(headers):
            record[h] = row[i] if i < len(row) else ""
        output.append(record)

    return output


def import_drive_csv(
    service_account_info: dict,
    file_id: str,
) -> List[Dict[str, str]]:
    creds = _creds(service_account_info)
    service = build("drive", "v3", credentials=creds)

    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()

    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)
    text = buffer.read().decode("utf-8").splitlines()
    reader = csv.DictReader(text)

    return list(reader)
