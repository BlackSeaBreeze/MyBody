"""
Форматы файлов для сохранения на Google Drive (Shorts / Detailed).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def drive_file_stem(*, timezone: str | None = None) -> str:
    """
    Имя файла без расширения: vb-YYYYMMDD-hhmm (24ч) в часовом поясе отчёта.
    По умолчанию REPORT_TIMEZONE=Europe/Dublin (как Cloud Scheduler).
    """
    tz_name = (timezone or os.environ.get("REPORT_TIMEZONE", "Europe/Dublin")).strip() or "Europe/Dublin"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Dublin")
    return datetime.now(tz).strftime("vb-%Y%m%d-%H%M")


def build_detailed_report_md(
    *,
    day_label: str,
    detailed_analysis: str,
    metrics: dict[str, Any],
    summary: dict[str, Any],
    model: str,
    context_meta: dict[str, Any] | None = None,
) -> str:
    """
    Markdown-отчёт для архива GCS: компактные метаданные + экспертный анализ Gemini
    (без сырых метрик — только выводы для последующего мета-анализа за неделю/месяц).
    """
    ctx = context_meta or {}
    generated_at = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_row = (summary.get("days") or [{}])[0] if summary.get("days") else {}

    frontmatter = {
        "date": day_label,
        "document_type": "mybody-daily-expert-analysis",
        "model": model,
        "generated_at_utc": generated_at,
        "purpose": "long_term_reanalysis",
        "compaction_level": ctx.get("compaction_level"),
        "input_est_tokens": ctx.get("est_tokens"),
        "key_metrics": {
            "steps": day_row.get("steps_value"),
            "step_goal": day_row.get("step_goal"),
            "sleep": day_row.get("sleep"),
            "sleep_seconds": day_row.get("sleep_seconds"),
            "calories_total": day_row.get("calories_total"),
            "calories_active": day_row.get("calories_active"),
            "distance_km": day_row.get("distance_km"),
            "stress_qualifier": day_row.get("stress"),
            "stress_avg": day_row.get("stress_avg"),
            "body_battery_wake": day_row.get("body_battery_wake"),
            "body_battery_end": day_row.get("body_battery_end"),
            "resting_hr": day_row.get("resting_hr"),
            "activities_count": len(metrics.get("activities") or []),
        },
    }

    meta_json = json.dumps(frontmatter, ensure_ascii=False, indent=2)

    return f"""# Экспертный дневной анализ MyBody — {day_label}

> Архив для мета-анализа (неделя / месяц). Только выводы эксперта, без сырых данных Garmin.

## Метаданные (JSON)

```json
{meta_json}
```

## Анализ

{detailed_analysis.strip()}
"""


def build_food_report_md(
    *,
    day_label: str,
    food_analysis: str,
    model: str,
    photo_meta: dict[str, Any] | None = None,
    context_meta: dict[str, Any] | None = None,
) -> str:
    """Markdown-отчёт по питанию для GCS archive/vb-…-food.md."""
    ctx = context_meta or {}
    pm = photo_meta or {}
    generated_at = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    frontmatter = {
        "date": day_label,
        "document_type": "mybody-daily-food-analysis",
        "model": model,
        "generated_at_utc": generated_at,
        "purpose": "nutrition_tracking_and_reanalysis",
        "photos_analyzed": ctx.get("photo_count") or pm.get("photo_count"),
        "drive_folder": pm.get("folder_name"),
        "photo_names": [p.get("name") for p in (pm.get("photos") or []) if p.get("name")],
    }

    meta_json = json.dumps(frontmatter, ensure_ascii=False, indent=2)

    return f"""# Анализ питания MyBody — {day_label}

> Архив нутриентов и экспертных выводов по фото еды. Без сырых изображений.

## Метаданные (JSON)

```json
{meta_json}
```

## Анализ

{food_analysis.strip()}
"""
