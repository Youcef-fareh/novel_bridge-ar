"""Upload generated EPUB files to the user's Google Drive."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_APP_FOLDER_NAME = "NovelBridge AR"


def _credentials_path() -> Path:
    return Path(os.getenv("GOOGLE_DRIVE_CREDENTIALS_FILE", "config/google_client_secret.json"))


def _token_path() -> Path:
    return Path(os.getenv("GOOGLE_DRIVE_TOKEN_FILE", str(Path.home() / ".novelbridge" / "google_drive_token.json")))


def _authorize():
    token_path = _token_path()
    credentials: Optional[Credentials] = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    elif not credentials or not credentials.valid:
        client_path = _credentials_path()
        if not client_path.exists():
            raise FileNotFoundError(
                f"Google Drive OAuth file not found: {client_path}. "
                "Download a Desktop OAuth client JSON from Google Cloud and place it there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), _SCOPES)
        credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def _find_or_create_folder(service):
    query = (
        "name = 'NovelBridge AR' and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    folders = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute().get("files", [])
    if folders:
        return folders[0]["id"]
    folder = service.files().create(
        body={"name": _APP_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return folder["id"]


def upload_epubs(
    output_dir: Path | str = "output",
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> int:
    """Upload all EPUB files in *output_dir* and return the upload count."""
    files = sorted(Path(output_dir).glob("*.epub"), key=lambda path: path.name.casefold())
    if not files:
        raise ValueError("No EPUB files were found in the output folder.")

    service = build("drive", "v3", credentials=_authorize(), cache_discovery=False)
    folder_id = _find_or_create_folder(service)
    total = len(files)
    uploaded = 0
    for index, path in enumerate(files, start=1):
        escaped_name = path.name.replace("'", "\\'")
        query = f"name = '{escaped_name}' and '{folder_id}' in parents and trashed = false"
        matches = service.files().list(q=query, spaces="drive", fields="files(id)").execute().get("files", [])
        media = MediaFileUpload(str(path), mimetype="application/epub+zip", resumable=True)
        if matches:
            service.files().update(fileId=matches[0]["id"], media_body=media).execute()
        else:
            service.files().create(
                body={"name": path.name, "parents": [folder_id]},
                media_body=media,
                fields="id",
            ).execute()
        uploaded += 1
        if progress_cb:
            progress_cb(index, total, f"Uploaded {path.name}")
    return uploaded