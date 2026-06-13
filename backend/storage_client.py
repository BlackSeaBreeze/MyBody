"""
Загрузка отчётов в Google Cloud Storage.

Аутентификация: Application Default Credentials (сервисный аккаунт Cloud Run).
SA нужна роль roles/storage.objectAdmin на bucket (см. scripts/setup_gcs_bucket.ps1).

Переменные:
    GCS_REPORTS_BUCKET    — имя bucket (обязательно)
    GCS_OUTCOMES_PREFIX   — префикс для HTML (по умолчанию outcomes/)
    GCS_ARCHIVE_PREFIX    — префикс для MD (по умолчанию archive/)
"""
from __future__ import annotations

import os
from typing import Any

try:
    from google.api_core import exceptions as gcp_exceptions
    from google.cloud import storage
except ImportError:
    storage = None  # type: ignore
    gcp_exceptions = None  # type: ignore


def is_configured() -> bool:
    if storage is None:
        return False
    return bool(os.environ.get("GCS_REPORTS_BUCKET", "").strip())


def _bucket_name() -> str:
    return os.environ.get("GCS_REPORTS_BUCKET", "").strip()


def _prefix(kind: str) -> str:
    if kind == "outcomes":
        env_key, default = "GCS_OUTCOMES_PREFIX", "outcomes/"
    else:
        env_key, default = "GCS_ARCHIVE_PREFIX", "archive/"
    raw = os.environ.get(env_key, default).strip() or default
    return raw if raw.endswith("/") else f"{raw}/"


def _client() -> storage.Client:
    return storage.Client()


def upload_text(
    *,
    object_name: str,
    content: str,
    content_type: str = "text/plain; charset=utf-8",
) -> dict[str, Any]:
    bucket_name = _bucket_name()
    if storage is None:
        return {"ok": False, "error": "gcs_sdk_not_installed"}
    if not bucket_name:
        return {"ok": False, "error": "gcs_bucket_not_configured"}

    data = content.encode("utf-8")
    try:
        bucket = _client().bucket(bucket_name)
        blob = bucket.blob(object_name)
        existed = blob.exists()
        blob.upload_from_string(data, content_type=content_type)
        return {
            "ok": True,
            "bucket": bucket_name,
            "object": object_name,
            "size_bytes": len(data),
            "gs_uri": f"gs://{bucket_name}/{object_name}",
            "updated": existed,
        }
    except Exception as e:
        status = getattr(e, "code", None)
        return {
            "ok": False,
            "error": "gcs_upload_failed",
            "detail": str(e),
            "http_status": status,
            "bucket": bucket_name,
            "object": object_name,
        }


def upload_to_outcomes(filename: str, content: str, content_type: str = "text/html; charset=utf-8") -> dict[str, Any]:
    return upload_text(object_name=f"{_prefix('outcomes')}{filename}", content=content, content_type=content_type)


def upload_to_archive(
    filename: str, content: str, content_type: str = "text/markdown; charset=utf-8"
) -> dict[str, Any]:
    return upload_text(object_name=f"{_prefix('archive')}{filename}", content=content, content_type=content_type)


def download_text(*, object_name: str) -> dict[str, Any]:
    """Скачивает объект из bucket как текст."""
    bucket_name = _bucket_name()
    if storage is None:
        return {"ok": False, "error": "gcs_sdk_not_installed"}
    if not bucket_name:
        return {"ok": False, "error": "gcs_bucket_not_configured"}

    try:
        bucket = _client().bucket(bucket_name)
        blob = bucket.blob(object_name)
        if not blob.exists():
            return {"ok": False, "error": "object_not_found", "object": object_name, "bucket": bucket_name}
        content = blob.download_as_text(encoding="utf-8")
        return {
            "ok": True,
            "bucket": bucket_name,
            "object": object_name,
            "content": content,
            "gs_uri": f"gs://{bucket_name}/{object_name}",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": "gcs_download_failed",
            "detail": str(e),
            "object": object_name,
            "bucket": bucket_name,
        }


def get_latest_food_analysis_for_day(day_label: str) -> dict[str, Any]:
    """
    Последний архивный food-отчёт за календарный день (YYYY-MM-DD).
    Имена: archive/vb-YYYYMMDD-HHMM-food.md; при нескольких — с максимальным HHMM в имени.
    """
    bucket_name = _bucket_name()
    if storage is None:
        return {"ok": False, "error": "gcs_sdk_not_installed"}
    if not bucket_name:
        return {"ok": False, "error": "gcs_bucket_not_configured"}

    day_compact = day_label.strip().replace("-", "")
    if len(day_compact) != 8 or not day_compact.isdigit():
        return {"ok": False, "error": "invalid_day_label", "day": day_label}

    list_prefix = f"{_prefix('archive')}vb-{day_compact}-"
    try:
        bucket = _client().bucket(bucket_name)
        candidates = [
            b.name
            for b in bucket.list_blobs(prefix=list_prefix)
            if b.name.endswith("-food.md")
        ]
        if not candidates:
            return {"ok": False, "error": "no_food_archive_for_day", "day": day_label}

        latest_object = max(candidates)
        downloaded = download_text(object_name=latest_object)
        if not downloaded.get("ok"):
            return downloaded
        return {
            "ok": True,
            "day": day_label,
            "object": latest_object,
            "gs_uri": downloaded.get("gs_uri"),
            "content": downloaded["content"],
            "candidates_count": len(candidates),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": "gcs_list_failed",
            "detail": str(e),
            "day": day_label,
            "bucket": bucket_name,
        }


def probe_access() -> dict[str, Any]:
    """Пробная запись и удаление объекта в bucket."""
    if storage is None:
        return {"ok": False, "error": "gcs_sdk_not_installed"}

    bucket_name = _bucket_name()
    if not bucket_name:
        return {"ok": False, "error": "gcs_bucket_not_configured"}

    probe_object = f".mybody-probe/{os.getpid()}-probe.txt"
    try:
        client = _client()
        bucket = client.bucket(bucket_name)
        try:
            bucket.reload()
        except gcp_exceptions.NotFound:
            return {"ok": False, "error": "bucket_not_found", "bucket": bucket_name}
        except Exception as e:
            return {"ok": False, "error": "bucket_access_failed", "detail": str(e), "bucket": bucket_name}

        write = upload_text(object_name=probe_object, content="mybody gcs probe", content_type="text/plain")
        if not write.get("ok"):
            return {"ok": False, "bucket": bucket_name, "write": write}

        bucket.blob(probe_object).delete()
        return {
            "ok": True,
            "bucket": bucket_name,
            "outcomes_prefix": _prefix("outcomes"),
            "archive_prefix": _prefix("archive"),
            "write": write,
            "probe_deleted": True,
        }
    except Exception as e:
        return {"ok": False, "error": "gcs_probe_failed", "detail": str(e), "bucket": bucket_name}
