"""
Загрузка файлов в Google Drive (папки Shorts / Detailed).

Аутентификация (по приоритету):
1. GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON — JSON сервисного аккаунта (Secret Manager).
2. Application Default Credentials (сервисный аккаунт Cloud Run).

Папки на «Мой диск» должны быть расшарены на email сервисного аккаунта
(Share → добавить SA с ролью Editor). См. README.

Переменные:
    DRIVE_SHORTS_FOLDER_ID    — папка Outcomes/Shorts
    DRIVE_DETAILED_FOLDER_ID  — папка Outcomes/Detailed
"""
from __future__ import annotations

import json
import os
from typing import Any

try:
    import httplib2
    import google.auth
    from google.oauth2 import service_account
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaInMemoryUpload
except ImportError:
    httplib2 = None  # type: ignore
    google = None  # type: ignore
    AuthorizedHttp = None  # type: ignore
    service_account = None  # type: ignore
    build = None  # type: ignore
    HttpError = None  # type: ignore
    MediaInMemoryUpload = None  # type: ignore

_DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)
_DRIVE_HTTP_TIMEOUT_SEC = 120


def is_configured() -> bool:
    """Drive API доступен и задан хотя бы один folder id."""
    if build is None:
        return False
    return bool(
        os.environ.get("DRIVE_SHORTS_FOLDER_ID", "").strip()
        or os.environ.get("DRIVE_DETAILED_FOLDER_ID", "").strip()
    )


def _credentials():
    raw = os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=_DRIVE_SCOPES)
    creds, _ = google.auth.default(scopes=_DRIVE_SCOPES)
    return creds


def _service():
    creds = _credentials()
    http = httplib2.Http(timeout=_DRIVE_HTTP_TIMEOUT_SEC)
    authorized = AuthorizedHttp(creds, http=http)
    return build("drive", "v3", http=authorized, cache_discovery=False)


def _find_file_id(service, folder_id: str, name: str) -> str | None:
    q = f"'{folder_id}' in parents and name = '{name.replace(chr(39), '')}' and trashed = false"
    resp = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = resp.get("files") or []
    return files[0]["id"] if files else None


def upload_text(
    *,
    folder_id: str,
    filename: str,
    content: str,
    mime_type: str = "text/plain; charset=utf-8",
) -> dict[str, Any]:
    """
    Создаёт или обновляет файл в папке Drive (по имени).
    Возвращает {"ok": True, "file_id": "...", "filename": "..."} или {"ok": False, "error": "..."}.
    """
    if build is None:
        return {"ok": False, "error": "drive_sdk_not_installed"}
    folder_id = folder_id.strip()
    if not folder_id:
        return {"ok": False, "error": "drive_folder_not_configured"}

    try:
        service = _service()
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type, resumable=False)
        existing_id = _find_file_id(service, folder_id, filename)
        if existing_id:
            updated = (
                service.files()
                .update(fileId=existing_id, media_body=media, fields="id,name,webViewLink")
                .execute()
            )
            return {
                "ok": True,
                "file_id": updated.get("id"),
                "filename": updated.get("name"),
                "webViewLink": updated.get("webViewLink"),
                "updated": True,
            }
        meta = {"name": filename, "parents": [folder_id]}
        created = service.files().create(body=meta, media_body=media, fields="id,name,webViewLink").execute()
        return {
            "ok": True,
            "file_id": created.get("id"),
            "filename": created.get("name"),
            "webViewLink": created.get("webViewLink"),
            "updated": False,
        }
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        return {
            "ok": False,
            "error": "drive_upload_failed",
            "detail": str(e),
            "http_status": status,
            "folder_id": folder_id,
            "filename": filename,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": "drive_upload_failed",
            "detail": str(e),
            "folder_id": folder_id,
            "filename": filename,
        }


def upload_to_shorts(filename: str, content: str, mime_type: str = "text/html; charset=utf-8") -> dict[str, Any]:
    folder = os.environ.get("DRIVE_SHORTS_FOLDER_ID", "").strip()
    return upload_text(folder_id=folder, filename=filename, content=content, mime_type=mime_type)


def upload_to_detailed(filename: str, content: str, mime_type: str = "text/markdown; charset=utf-8") -> dict[str, Any]:
    folder = os.environ.get("DRIVE_DETAILED_FOLDER_ID", "").strip()
    return upload_text(folder_id=folder, filename=filename, content=content, mime_type=mime_type)
