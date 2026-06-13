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


def _credential_email(creds) -> str | None:
    for attr in ("service_account_email", "signer_email"):
        val = getattr(creds, attr, None)
        if val:
            return str(val)
    return None


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


def upload_to_shorts(filename: str, content: str, mime_type: str = "text/html") -> dict[str, Any]:
    folder = os.environ.get("DRIVE_SHORTS_FOLDER_ID", "").strip()
    return upload_text(folder_id=folder, filename=filename, content=content, mime_type=mime_type)


def upload_to_detailed(filename: str, content: str, mime_type: str = "text/markdown") -> dict[str, Any]:
    folder = os.environ.get("DRIVE_DETAILED_FOLDER_ID", "").strip()
    return upload_text(folder_id=folder, filename=filename, content=content, mime_type=mime_type)


def _probe_one_folder(service, folder_id: str) -> dict[str, Any]:
    probe_name = ".mybody-drive-probe.txt"
    out: dict[str, Any] = {"folder_id": folder_id}
    try:
        meta = service.files().get(fileId=folder_id, fields="id,name,mimeType").execute()
        out["folder_name"] = meta.get("name")
        out["folder_visible"] = True
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        out["folder_visible"] = False
        out["folder_error"] = str(e)
        out["http_status"] = status
        out["ok"] = False
        return out

    try:
        service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            pageSize=1,
            fields="files(id)",
        ).execute()
        out["list_ok"] = True
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        out["list_ok"] = False
        out["list_error"] = str(e)
        out["http_status"] = status
        out["ok"] = False
        return out

    created = upload_text(
        folder_id=folder_id,
        filename=probe_name,
        content="mybody drive probe",
        mime_type="text/plain",
    )
    out["write"] = created
    if not created.get("ok"):
        out["ok"] = False
        return out

    try:
        service.files().delete(fileId=created["file_id"]).execute()
        out["probe_deleted"] = True
    except HttpError as e:
        out["probe_deleted"] = False
        out["delete_error"] = str(e)

    out["ok"] = True
    return out


def probe_access() -> dict[str, Any]:
    """Диагностика Drive: SA email и пробная запись в Shorts/Detailed."""
    if build is None:
        return {"ok": False, "error": "drive_sdk_not_installed"}

    auth_source = "json" if os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", "").strip() else "adc"
    try:
        creds = _credentials()
        sa_email = _credential_email(creds)
        service = _service()
    except Exception as e:
        return {"ok": False, "error": "credentials_failed", "detail": str(e), "auth_source": auth_source}

    result: dict[str, Any] = {
        "ok": True,
        "auth_source": auth_source,
        "service_account_email": sa_email,
        "shorts_folder_id": os.environ.get("DRIVE_SHORTS_FOLDER_ID", "").strip(),
        "detailed_folder_id": os.environ.get("DRIVE_DETAILED_FOLDER_ID", "").strip(),
    }

    checks: list[bool] = []
    for key, env_name in (("shorts", "DRIVE_SHORTS_FOLDER_ID"), ("detailed", "DRIVE_DETAILED_FOLDER_ID")):
        folder_id = os.environ.get(env_name, "").strip()
        if not folder_id:
            result[key] = {"ok": False, "error": "folder_id_not_set"}
            continue
        probe = _probe_one_folder(service, folder_id)
        result[key] = probe
        checks.append(bool(probe.get("ok")))

    result["ok"] = bool(checks) and all(checks)
    return result
