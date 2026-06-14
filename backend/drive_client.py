"""
Загрузка файлов в Google Drive (папки Shorts / Detailed).

Аутентификация (по приоритету):
1. GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON — JSON сервисного аккаунта (Secret Manager).
2. Application Default Credentials (сервисный аккаунт Cloud Run).

Папки должны находиться на **Shared drive** (общий диск), не на «Мой диск».
Сервисный аккаунт добавляют участником Shared drive (Content manager / Contributor).
Share отдельной папки на «Мой диск» даёт list/get, но create падает с storageQuotaExceeded.

Переменные:
    DRIVE_SHORTS_FOLDER_ID    — папка Outcomes/Shorts
    DRIVE_DETAILED_FOLDER_ID  — папка Outcomes/Detailed
    DRIVE_MEALS_FOLDER_ID     — корневая папка Meals (чтение фото; подпапки YYYY.MM.DD)
    DRIVE_NOTES_FOLDER_ID     — папка Notes (файлы YYYY.MM.DD — Google Docs или текст)
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
    from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload
except ImportError:
    httplib2 = None  # type: ignore
    google = None  # type: ignore
    AuthorizedHttp = None  # type: ignore
    service_account = None  # type: ignore
    build = None  # type: ignore
    HttpError = None  # type: ignore
    MediaInMemoryUpload = None  # type: ignore
    MediaIoBaseDownload = None  # type: ignore

_DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)
_DRIVE_HTTP_TIMEOUT_SEC = 120
# Shared drive API flags (обязательны для записи от сервисного аккаунта)
_LIST_KW = {"supportsAllDrives": True, "includeItemsFromAllDrives": True}
_FILE_KW = {"supportsAllDrives": True}


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
        if val and str(val) != "default":
            return str(val)
    inner = getattr(creds, "_credentials", None) or getattr(creds, "credentials", None)
    if inner is not None and inner is not creds:
        return _credential_email(inner)
    try:
        import urllib.request

        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.read().decode().strip()
    except Exception:
        return None


def _service():
    creds = _credentials()
    http = httplib2.Http(timeout=_DRIVE_HTTP_TIMEOUT_SEC)
    authorized = AuthorizedHttp(creds, http=http)
    return build("drive", "v3", http=authorized, cache_discovery=False)


def _find_file_id(service, folder_id: str, name: str) -> str | None:
    q = f"'{folder_id}' in parents and name = '{name.replace(chr(39), '')}' and trashed = false"
    resp = service.files().list(q=q, fields="files(id)", pageSize=1, **_LIST_KW).execute()
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
                .update(fileId=existing_id, media_body=media, fields="id,name,webViewLink", **_FILE_KW)
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
        created = (
            service.files()
            .create(body=meta, media_body=media, fields="id,name,webViewLink", **_FILE_KW)
            .execute()
        )
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
        meta = service.files().get(fileId=folder_id, fields="id,name,mimeType,driveId", **_FILE_KW).execute()
        out["folder_name"] = meta.get("name")
        out["on_shared_drive"] = bool(meta.get("driveId"))
        out["folder_visible"] = True
        if not meta.get("driveId"):
            out["hint"] = (
                "Папка на «Мой диск». Сервисный аккаунт не может создавать файлы там. "
                "Перенесите папку на Shared drive и добавьте SA участником (Content manager)."
            )
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
            **_LIST_KW,
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
        service.files().delete(fileId=created["file_id"], **_FILE_KW).execute()
        out["probe_deleted"] = True
    except HttpError as e:
        out["probe_deleted"] = False
        out["delete_error"] = str(e)

    out["ok"] = True
    return out


def is_meals_configured() -> bool:
    """Drive API доступен и задан id папки Meals."""
    if build is None:
        return False
    return bool(os.environ.get("DRIVE_MEALS_FOLDER_ID", "").strip())


def _meals_folder_id() -> str:
    return os.environ.get("DRIVE_MEALS_FOLDER_ID", "").strip()


def meals_subfolder_name(day: str) -> str:
    """ISO-дата YYYY-MM-DD → метка дня на Drive (YYYY.MM.DD) — подпапка Meals или имя файла Notes."""
    return day.strip().replace("-", ".")


def is_notes_configured() -> bool:
    """Drive API доступен и задан id папки Notes."""
    if build is None:
        return False
    return bool(os.environ.get("DRIVE_NOTES_FOLDER_ID", "").strip())


def _notes_folder_id() -> str:
    return os.environ.get("DRIVE_NOTES_FOLDER_ID", "").strip()


_GOOGLE_EXPORT_MIMES: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}


def _notes_max_chars() -> int:
    try:
        return max(1000, int(os.environ.get("DRIVE_NOTES_MAX_CHARS", "32000")))
    except ValueError:
        return 32000


def _read_drive_file_text(service, file_id: str, mime_type: str) -> tuple[str | None, str | None]:
    """Читает текст файла Drive (Google Docs через export, обычные — download)."""
    export_mime = _GOOGLE_EXPORT_MIMES.get(mime_type or "")
    try:
        if export_mime:
            raw = service.files().export(fileId=file_id, mimeType=export_mime).execute()
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", errors="replace")
            else:
                text = str(raw)
            return text, None
        downloaded = download_file_bytes(file_id)
        if not downloaded.get("ok"):
            return None, downloaded.get("error") or "drive_download_failed"
        data = downloaded.get("data") or b""
        return data.decode("utf-8", errors="replace"), None
    except HttpError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def fetch_day_notes(day: str) -> dict[str, Any]:
    """
    Загружает заметки за день из папки Notes: файл с именем YYYY.MM.DD (Google Docs или текст).
    """
    if build is None:
        return {"ok": False, "error": "drive_sdk_not_installed"}
    root_id = _notes_folder_id()
    if not root_id:
        return {"ok": False, "error": "drive_notes_folder_not_configured"}

    filename = meals_subfolder_name(day)
    out: dict[str, Any] = {
        "ok": True,
        "day": day,
        "filename": filename,
        "notes_root_id": root_id,
        "file_found": False,
        "text": None,
    }

    try:
        service = _service()
        file_id = _find_file_id(service, root_id, filename)
        if not file_id:
            return out

        meta = (
            service.files()
            .get(fileId=file_id, fields="id,name,mimeType,size", **_FILE_KW)
            .execute()
        )
        mime_type = meta.get("mimeType") or ""
        text, read_err = _read_drive_file_text(service, file_id, mime_type)
        if read_err:
            return {
                "ok": False,
                "error": "drive_notes_read_failed",
                "detail": read_err,
                "day": day,
                "filename": filename,
                "file_id": file_id,
                "mime_type": mime_type,
            }
        if text is None:
            text = ""

        max_chars = _notes_max_chars()
        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(заметки обрезаны по лимиту)…"
            truncated = True

        out.update(
            {
                "file_found": True,
                "file_id": file_id,
                "mime_type": mime_type,
                "size": int(meta.get("size") or 0),
                "char_count": len(text),
                "truncated": truncated,
                "text": text.strip(),
            }
        )
        return out
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        return {
            "ok": False,
            "error": "drive_notes_fetch_failed",
            "detail": str(e),
            "http_status": status,
            "day": day,
            "filename": filename,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": "drive_notes_fetch_failed",
            "detail": str(e),
            "day": day,
            "filename": filename,
        }


def probe_notes_access(day: str | None = None) -> dict[str, Any]:
    """Диагностика чтения папки Notes и файла за день."""
    if build is None:
        return {"ok": False, "error": "drive_sdk_not_installed"}

    root_id = _notes_folder_id()
    if not root_id:
        return {"ok": False, "error": "drive_notes_folder_not_configured"}

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
        "notes_folder_id": root_id,
    }

    try:
        meta = service.files().get(fileId=root_id, fields="id,name,mimeType", **_FILE_KW).execute()
        result["notes_folder_name"] = meta.get("name")
        result["notes_folder_visible"] = True
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        result["ok"] = False
        result["notes_folder_visible"] = False
        result["error"] = str(e)
        result["http_status"] = status
        return result

    if day:
        fetch = fetch_day_notes(day)
        preview = (fetch.get("text") or "")[:200]
        result["day_probe"] = {
            "day": day,
            "filename": fetch.get("filename"),
            "file_found": fetch.get("file_found", False),
            "char_count": fetch.get("char_count", 0),
            "mime_type": fetch.get("mime_type"),
            "preview": preview,
            "ok": fetch.get("ok"),
            "error": fetch.get("error"),
        }
        if not fetch.get("ok"):
            result["ok"] = False

    return result


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_child_folder(service, parent_id: str, name: str) -> str | None:
    escaped = _escape_drive_query(name)
    q = (
        f"'{parent_id}' in parents and name = '{escaped}' "
        f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resp = service.files().list(q=q, fields="files(id)", pageSize=1, **_LIST_KW).execute()
    files = resp.get("files") or []
    return files[0]["id"] if files else None


def _list_folder_files(service, folder_id: str) -> list[dict[str, Any]]:
    q = f"'{folder_id}' in parents and trashed = false"
    files: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        resp = (
            service.files()
            .list(
                q=q,
                fields="nextPageToken,files(id,name,mimeType,size,createdTime)",
                pageSize=100,
                pageToken=page_token,
                **_LIST_KW,
            )
            .execute()
        )
        files.extend(resp.get("files") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


_IMAGE_MIMES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
})


def _is_meal_image(mime_type: str | None) -> bool:
    mime = (mime_type or "").strip().lower()
    if not mime or mime == "application/vnd.google-apps.folder":
        return False
    if mime in _IMAGE_MIMES:
        return True
    return mime.startswith("image/")

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore


def _meals_fetch_limits() -> dict[str, int]:
    def _int(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, str(default))))
        except ValueError:
            return default

    return {
        "max_photos": _int("DRIVE_MEALS_MAX_PHOTOS", 24),
        "max_bytes_per_file": _int("DRIVE_MEALS_MAX_BYTES", 12_000_000),
        "max_total_bytes": _int("DRIVE_MEALS_MAX_TOTAL_BYTES", 80_000_000),
        "max_download_bytes": _int("DRIVE_MEALS_MAX_DOWNLOAD_BYTES", 20_000_000),
        "compress_if_bytes": _int("MEALS_COMPRESS_IF_BYTES", 4_000_000),
        "compress_soft_edge": _int("MEALS_COMPRESS_SOFT_EDGE", 2560),
        "compress_soft_quality": _int("MEALS_COMPRESS_SOFT_QUALITY", 92),
        "compress_hard_edge": _int("MEALS_COMPRESS_HARD_EDGE", 1920),
        "compress_hard_quality": _int("MEALS_COMPRESS_HARD_QUALITY", 88),
    }


def _encode_meal_image(
    data: bytes,
    mime_type: str,
    *,
    max_edge: int,
    quality: int,
) -> tuple[bytes, str, int]:
    """Сжимает фото только при необходимости (resize + JPEG)."""
    if Image is None or not data:
        return data, mime_type, len(data)
    try:
        import io

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if max(img.size) > max_edge:
                img = img.copy()
                img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            return out, "image/jpeg", len(out)
    except Exception:
        return data, mime_type, len(data)


def _gemini_inline_max_bytes() -> int:
    try:
        return max(500_000, int(os.environ.get("GEMINI_INLINE_IMAGE_MAX_BYTES", "7000000")))
    except ValueError:
        return 7_000_000


def prepare_photo_for_gemini(data: bytes, mime_type: str) -> tuple[bytes, str, str]:
    """
    Укладывает фото в лимит inline Gemini API (~7 MB на файл).
    Возвращает (bytes, mime_type, adjustment): none | soft | hard | gemini.
    """
    max_b = _gemini_inline_max_bytes()
    if not data or len(data) <= max_b:
        return data, mime_type, "none"

    limits = _meals_fetch_limits()
    last: tuple[bytes, str, int] = (data, mime_type, len(data))
    for label, edge, quality in (
        ("soft", limits["compress_soft_edge"], limits["compress_soft_quality"]),
        ("hard", limits["compress_hard_edge"], limits["compress_hard_quality"]),
        ("gemini", 1280, 85),
        ("gemini", 1280, 78),
    ):
        last = _encode_meal_image(data, mime_type, max_edge=edge, quality=quality)
        if last[2] <= max_b:
            return last[0], last[1], label
    return last[0], last[1], "gemini"


def _prepare_meal_image(
    data: bytes,
    mime_type: str,
    *,
    limits: dict[str, int],
    remaining_budget: int,
    max_bytes_per_file: int,
) -> tuple[bytes | None, str, int, str]:
    """
    Возвращает (bytes, mime, size, compression).
    compression: none | soft | hard | skipped_too_large
    По умолчанию (MEALS_IMAGE_COMPRESS=auto) оригинал сохраняется, если влезает в лимиты.
    """
    size = len(data)
    mode = (os.environ.get("MEALS_IMAGE_COMPRESS") or "auto").strip().lower()

    def fits(n: int) -> bool:
        return n <= remaining_budget and n <= max_bytes_per_file

    if mode == "never":
        return (data, mime_type, size, "none") if fits(size) else (None, mime_type, size, "skipped_too_large")

    if mode != "always" and fits(size) and size <= limits["compress_if_bytes"]:
        return data, mime_type, size, "none"

    last: tuple[bytes, str, int] = (data, mime_type, size)
    for label, edge, quality in (
        ("soft", limits["compress_soft_edge"], limits["compress_soft_quality"]),
        ("hard", limits["compress_hard_edge"], limits["compress_hard_quality"]),
    ):
        last = _encode_meal_image(data, mime_type, max_edge=edge, quality=quality)
        if fits(last[2]):
            return last[0], last[1], last[2], label

    if fits(last[2]):
        return last[0], last[1], last[2], "hard"
    return None, mime_type, last[2], "skipped_too_large"


def download_file_bytes(file_id: str) -> dict[str, Any]:
    """Скачивает содержимое файла Drive."""
    if build is None or MediaIoBaseDownload is None:
        return {"ok": False, "error": "drive_sdk_not_installed"}
    try:
        import io

        service = _service()
        meta = (
            service.files()
            .get(fileId=file_id, fields="id,name,mimeType,size", **_FILE_KW)
            .execute()
        )
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return {
            "ok": True,
            "file_id": file_id,
            "name": meta.get("name"),
            "mime_type": meta.get("mimeType"),
            "size": int(meta.get("size") or 0),
            "data": buf.getvalue(),
        }
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        return {"ok": False, "error": "drive_download_failed", "detail": str(e), "http_status": status}
    except Exception as e:
        return {"ok": False, "error": "drive_download_failed", "detail": str(e)}


def fetch_day_meal_photos(
    day: str,
    *,
    max_photos: int | None = None,
    max_bytes_per_file: int | None = None,
) -> dict[str, Any]:
    """
    Загружает фото еды из подпапки Meals/YYYY.MM.DD на Google Drive.
    Возвращает список фото с байтами для передачи в Gemini Vision.
    По умолчанию фото не сжимаются; сжатие только если файл > MEALS_COMPRESS_IF_BYTES
    или не влезает в DRIVE_MEALS_MAX_BYTES / DRIVE_MEALS_MAX_TOTAL_BYTES.
    """
    if build is None:
        return {"ok": False, "error": "drive_sdk_not_installed"}
    root_id = _meals_folder_id()
    if not root_id:
        return {"ok": False, "error": "drive_meals_folder_not_configured"}

    limits = _meals_fetch_limits()
    max_photos = max_photos if max_photos is not None else limits["max_photos"]
    max_bytes_per_file = max_bytes_per_file if max_bytes_per_file is not None else limits["max_bytes_per_file"]
    max_total_bytes = limits["max_total_bytes"]
    max_download_bytes = limits["max_download_bytes"]

    folder_name = meals_subfolder_name(day)
    out: dict[str, Any] = {
        "ok": True,
        "day": day,
        "folder_name": folder_name,
        "meals_root_id": root_id,
        "photos": [],
        "skipped": [],
        "limits": {
            "max_photos": max_photos,
            "max_bytes_per_file": max_bytes_per_file,
            "max_total_bytes": max_total_bytes,
            "max_download_bytes": max_download_bytes,
            "compress_if_bytes": limits["compress_if_bytes"],
            "compress_mode": (os.environ.get("MEALS_IMAGE_COMPRESS") or "auto").strip().lower(),
        },
    }

    try:
        service = _service()
        day_folder_id = _find_child_folder(service, root_id, folder_name)
        if not day_folder_id:
            out["folder_found"] = False
            out["photo_count"] = 0
            return out

        out["folder_found"] = True
        out["folder_id"] = day_folder_id
        entries = _list_folder_files(service, day_folder_id)
        out["files_in_folder"] = [
            {"name": f.get("name"), "mimeType": f.get("mimeType"), "size": f.get("size")}
            for f in entries
        ]
        image_entries = [f for f in entries if _is_meal_image(f.get("mimeType"))]
        out["non_image_files"] = [
            {"name": f.get("name"), "mimeType": f.get("mimeType"), "size": f.get("size")}
            for f in entries
            if not _is_meal_image(f.get("mimeType"))
        ]
        image_entries.sort(key=lambda f: (f.get("name") or f.get("createdTime") or ""))

        photos: list[dict[str, Any]] = []
        total_bytes = 0
        for entry in image_entries:
            if len(photos) >= max_photos:
                out["skipped"].append({"name": entry.get("name"), "reason": "max_photos_reached"})
                continue
            size = int(entry.get("size") or 0)
            if size > max_download_bytes:
                out["skipped"].append(
                    {"name": entry.get("name"), "reason": "file_too_large", "size": size, "limit": max_download_bytes}
                )
                continue
            downloaded = download_file_bytes(entry["id"])
            if not downloaded.get("ok"):
                out["skipped"].append(
                    {"name": entry.get("name"), "reason": "download_failed", "error": downloaded.get("error")}
                )
                continue
            raw_data = downloaded["data"]
            raw_mime = downloaded.get("mime_type") or entry.get("mimeType") or "image/jpeg"
            remaining_budget = max_total_bytes - total_bytes
            data, mime_type, final_size, compression = _prepare_meal_image(
                raw_data,
                raw_mime,
                limits=limits,
                remaining_budget=remaining_budget,
                max_bytes_per_file=max_bytes_per_file,
            )
            del raw_data
            if data is None or compression == "skipped_too_large":
                reason = "max_total_bytes_reached" if total_bytes > 0 else "file_too_large_after_compress"
                out["skipped"].append(
                    {
                        "name": entry.get("name"),
                        "reason": reason,
                        "size": final_size,
                        "size_raw": downloaded.get("size") or size,
                        "total_bytes": total_bytes,
                        "remaining_budget": remaining_budget,
                    }
                )
                continue
            total_bytes += final_size
            photos.append(
                {
                    "file_id": entry["id"],
                    "name": downloaded.get("name") or entry.get("name"),
                    "mime_type": mime_type,
                    "size": final_size,
                    "size_raw": downloaded.get("size") or size,
                    "compression": compression,
                    "data": data,
                }
            )

        out["photos"] = photos
        out["images_found"] = len(image_entries)
        out["photo_count"] = len(photos)
        out["photos_skipped_count"] = len(out["skipped"])
        out["analysis_complete"] = len(out["skipped"]) == 0 and len(photos) == len(image_entries)
        out["total_bytes"] = total_bytes
        return out
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        return {
            "ok": False,
            "error": "drive_meals_fetch_failed",
            "detail": str(e),
            "http_status": status,
            "day": day,
            "folder_name": folder_name,
        }
    except Exception as e:
        return {
            "ok": False,
            "error": "drive_meals_fetch_failed",
            "detail": str(e),
            "day": day,
            "folder_name": folder_name,
        }


def probe_meals_access(day: str | None = None) -> dict[str, Any]:
    """Диагностика чтения папки Meals и подпапки за день."""
    if build is None:
        return {"ok": False, "error": "drive_sdk_not_installed"}

    root_id = _meals_folder_id()
    if not root_id:
        return {"ok": False, "error": "drive_meals_folder_not_configured"}

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
        "meals_folder_id": root_id,
    }

    try:
        meta = service.files().get(fileId=root_id, fields="id,name,mimeType", **_FILE_KW).execute()
        result["meals_folder_name"] = meta.get("name")
        result["meals_folder_visible"] = True
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        result["ok"] = False
        result["meals_folder_visible"] = False
        result["error"] = str(e)
        result["http_status"] = status
        return result

    if day:
        fetch = fetch_day_meal_photos(day)
        result["day_probe"] = {
            "day": day,
            "folder_name": fetch.get("folder_name"),
            "folder_found": fetch.get("folder_found", False),
            "files_in_folder": fetch.get("files_in_folder"),
            "non_image_files": fetch.get("non_image_files"),
            "images_found": fetch.get("images_found", 0),
            "photo_count": fetch.get("photo_count", 0),
            "photos_skipped_count": fetch.get("photos_skipped_count", 0),
            "analysis_complete": fetch.get("analysis_complete"),
            "skipped": fetch.get("skipped"),
            "ok": fetch.get("ok"),
            "error": fetch.get("error"),
        }
        if not fetch.get("ok"):
            result["ok"] = False

    return result


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
