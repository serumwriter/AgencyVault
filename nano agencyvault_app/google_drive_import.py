from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import pandas as pd
import io


def import_google_sheet(creds_dict, file_id):
    creds = Credentials.from_authorized_user_info(creds_dict)
    service = build("drive", "v3", credentials=creds)

    request = service.files().export_media(
        fileId=file_id,
        mimeType="text/csv"
    )
    data = request.execute()

    df = pd.read_csv(io.BytesIO(data))
    return df


def import_drive_csv(creds_dict, file_id):
    creds = Credentials.from_authorized_user_info(creds_dict)
    service = build("drive", "v3", credentials=creds)

    request = service.files().get_media(fileId=file_id)
    data = request.execute()

    df = pd.read_csv(io.BytesIO(data))
    return df
