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
    Markdown-отчёт для папки Detailed: максимум структуры для последующего
    повторного анализа Gemini за неделю/месяц.
    """
    ctx = context_meta or {}
    generated_at = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_row = (summary.get("days") or [{}])[0] if summary.get("days") else {}

    frontmatter = {
        "date": day_label,
        "document_type": "mybody-daily-detailed",
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

    metrics_snapshot = {
        "from": metrics.get("from"),
        "to": metrics.get("to"),
        "activities": metrics.get("activities") or [],
        "metrics_by_day": metrics.get("metrics_by_day") or {},
        "range_metrics": metrics.get("range_metrics") or {},
        "global_metrics": metrics.get("global_metrics") or {},
    }

    meta_json = json.dumps(frontmatter, ensure_ascii=False, indent=2)
    snap_json = json.dumps(metrics_snapshot, ensure_ascii=False, indent=2, default=str)

    return f"""# Подробный дневной отчёт MyBody — {day_label}

> Архив для мета-анализа (неделя / месяц). Не удаляйте секции при объединении файлов.

## Метаданные (JSON)

```json
{meta_json}
```

## Анализ Gemini (архивный, максимальная детализация)

{detailed_analysis.strip()}

---

## Снимок сырых метрик Garmin (JSON)

```json
{snap_json}
```
"""
