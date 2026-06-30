"""
Форматы файлов для сохранения на Google Drive (Shorts / Detailed) и HTML для email.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def drive_file_stem(*, day_label: str | None = None, timezone: str | None = None) -> str:
    """
    Имя файла без расширения: vb-YYYYMMDD-hhmm (24ч) в часовом поясе отчёта.
    YYYYMMDD — календарный день отчёта (day_label), hhmm — время запуска.
    """
    tz_name = (timezone or os.environ.get("REPORT_TIMEZONE", "Europe/Dublin")).strip() or "Europe/Dublin"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Dublin")
    now = datetime.now(tz)
    if day_label:
        date_part = day_label.strip().replace("-", "")
    else:
        date_part = now.strftime("%Y%m%d")
    return f"vb-{date_part}-{now.strftime('%H%M')}"


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
    vo2 = summary.get("vo2_max") or {}
    sleep_km = metrics.get("sleep_report") if isinstance(metrics.get("sleep_report"), dict) else {}

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
            "vo2_max": vo2.get("display"),
            "vo2_max_generic": vo2.get("generic"),
            "vo2_max_cycling": vo2.get("cycling"),
            "sleep_score": sleep_km.get("sleep_score_overall"),
            "restless_moments": sleep_km.get("restless_moments"),
            "sleep_quality": sleep_km.get("sleep_quality_label"),
            "avg_hr_sleep": sleep_km.get("avg_hr_sleep"),
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
        "photos_found_in_folder": pm.get("images_found") or pm.get("photo_count"),
        "photos_analyzed": ctx.get("photo_count") or pm.get("photo_count"),
        "photos_skipped_count": pm.get("photos_skipped_count") or len(pm.get("skipped") or []),
        "analysis_complete": pm.get("analysis_complete", True),
        "drive_folder": pm.get("folder_name"),
        "photo_names": [p.get("name") for p in (pm.get("photos") or []) if p.get("name")],
        "photos_skipped": pm.get("skipped") or [],
        "non_image_files": pm.get("non_image_files") or [],
        "files_in_folder_count": len(pm.get("files_in_folder") or []),
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


def extract_analysis_section(md: str) -> str:
    """Текст секции «## Анализ» из архивного Markdown (без метаданных)."""
    marker = "## Анализ"
    idx = md.find(marker)
    if idx == -1:
        return md.strip()
    return md[idx + len(marker) :].strip()


def extract_food_frontmatter(md: str) -> dict[str, Any]:
    """JSON из блока метаданных food-отчёта; пустой dict если не найден."""
    return extract_archive_frontmatter(md)


def extract_archive_frontmatter(md: str) -> dict[str, Any]:
    """JSON из первого ```json блока в архивном Markdown."""
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", md)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def extract_markdown_section(md: str, heading: str) -> str:
    """Текст секции ## heading до следующей ## или конца документа."""
    pattern = rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    m = re.search(pattern, md, flags=re.I | re.M)
    return m.group(1).strip() if m else ""


def extract_fact_lines(text: str) -> list[str]:
    """Строки FACT | … из секции facts_for_aggregation или всего текста."""
    facts: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^-\s*FACT\s*\|", s, flags=re.I):
            facts.append(re.sub(r"^-\s*", "", s))
        elif s.startswith("FACT |"):
            facts.append(s)
    return facts


_CHARS_PER_TOKEN_EST = 1.35


def estimate_token_count(text: str) -> int:
    """Оценка числа токенов (эмпирика Garmin/JSON ~1.35 символа/токен)."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN_EST)


def _weekly_input_token_budget() -> int:
    try:
        return max(10_000, int(os.environ.get("WEEKLY_MAX_INPUT_TOKENS", "220000")))
    except ValueError:
        return 220_000


def weekly_file_stem(*, end_day_label: str, timezone: str | None = None) -> str:
    """Имя weekly-отчёта: weekly-vb-YYYYMMDD-hhmm."""
    tz_name = (timezone or os.environ.get("REPORT_TIMEZONE", "Europe/Dublin")).strip() or "Europe/Dublin"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Dublin")
    now = datetime.now(tz)
    date_part = end_day_label.strip().replace("-", "")
    return f"weekly-vb-{date_part}-{now.strftime('%H%M')}"


def build_weekly_full_context(
    *,
    garmin_archives: list[dict[str, Any]],
    food_archives: list[dict[str, Any]],
) -> str:
    """Склеивает полные сохранённые MD-архивы за период (Garmin + food по дням)."""
    food_by_day = {str(a.get("day")): a for a in food_archives if a.get("day")}
    parts: list[str] = []

    for arch in sorted(garmin_archives, key=lambda a: str(a.get("day") or "")):
        day = str(arch.get("day") or "?")
        obj = arch.get("object") or ""
        parts.append(
            f"=== {day} — экспертный архив Garmin ({obj}) ===\n"
            f"{(arch.get('content') or '').strip()}"
        )
        food = food_by_day.get(day)
        if food and food.get("content"):
            parts.append(
                f"=== {day} — архив питания ({food.get('object') or ''}) ===\n"
                f"{food['content'].strip()}"
            )

    return "\n\n".join(parts)


def build_weekly_fact_digest(
    *,
    garmin_archives: list[dict[str, Any]],
    food_archives: list[dict[str, Any]],
) -> str:
    """
    Сценарий 1 (FACT-digest): key_metrics + sleep_metrics + FACT-строки по каждому дню.
    """
    food_by_day = {str(a.get("day")): a for a in food_archives if a.get("day")}
    days_payload: list[dict[str, Any]] = []

    for arch in sorted(garmin_archives, key=lambda a: str(a.get("day") or "")):
        day = str(arch.get("day") or "?")
        content = arch.get("content") or ""
        fm = extract_archive_frontmatter(content)
        analysis = extract_analysis_section(content)
        day_entry: dict[str, Any] = {
            "date": fm.get("date") or day,
            "garmin_object": arch.get("object"),
            "key_metrics": fm.get("key_metrics") or {},
            "sleep_metrics": extract_markdown_section(analysis, "sleep_metrics"),
            "facts": extract_fact_lines(analysis),
        }
        food = food_by_day.get(day)
        if food and food.get("content"):
            food_content = food["content"]
            food_fm = extract_archive_frontmatter(food_content)
            food_analysis = extract_analysis_section(food_content)
            day_entry["food"] = {
                "object": food.get("object"),
                "meta": {
                    k: food_fm.get(k)
                    for k in (
                        "photos_analyzed",
                        "analysis_complete",
                        "photos_found_in_folder",
                    )
                    if k in food_fm
                },
                "daily_totals": extract_markdown_section(food_analysis, "daily_totals"),
                "facts": extract_fact_lines(food_analysis),
            }
        days_payload.append(day_entry)

    payload = {
        "document_type": "mybody-weekly-fact-digest",
        "purpose": "gemini_weekly_input_reduced",
        "days_count": len(days_payload),
        "days": days_payload,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def prepare_weekly_gemini_context(
    *,
    garmin_archives: list[dict[str, Any]],
    food_archives: list[dict[str, Any]],
    max_input_tokens: int | None = None,
) -> dict[str, Any]:
    """
    Полные архивы, если влезают в бюджет; иначе FACT-digest (сценарий 1).
    """
    budget = max_input_tokens if max_input_tokens is not None else _weekly_input_token_budget()
    full_text = build_weekly_full_context(
        garmin_archives=garmin_archives,
        food_archives=food_archives,
    )
    est_full = estimate_token_count(full_text)
    if est_full <= budget:
        return {
            "text": full_text,
            "context_mode": "full",
            "est_tokens": est_full,
            "est_tokens_full": est_full,
            "free_tier_fallback": False,
            "input_token_budget": budget,
        }

    digest_text = build_weekly_fact_digest(
        garmin_archives=garmin_archives,
        food_archives=food_archives,
    )
    est_digest = estimate_token_count(digest_text)
    return {
        "text": digest_text,
        "context_mode": "fact_digest",
        "est_tokens": est_digest,
        "est_tokens_full": est_full,
        "free_tier_fallback": True,
        "input_token_budget": budget,
    }


def weekly_context_disclaimer_html(ctx: dict[str, Any]) -> str:
    """Баннер в письме, если контекст был сокращён перед Gemini."""
    if not ctx.get("free_tier_fallback"):
        return ""
    est_full = ctx.get("est_tokens_full")
    est_used = ctx.get("est_tokens")
    budget = ctx.get("input_token_budget")
    full_s = f"{est_full:,}".replace(",", " ") if est_full is not None else "?"
    used_s = f"{est_used:,}".replace(",", " ") if est_used is not None else "?"
    budget_s = f"{budget:,}".replace(",", " ") if budget is not None else "220 000"
    return (
        '<div style="background:#fff8e6;border:1px solid #f0c040;padding:12px 14px;'
        'margin:0 0 18px;border-radius:6px;font-size:14px;color:#5c4a00;">'
        "<strong>⚠ Контекст для Gemini был сокращён.</strong> "
        f"Полные архивы за период (~{full_s} токенов) превысили лимит входа "
        f"({budget_s} токенов, free tier). В модель переданы агрегированные метрики "
        f"(FACT-digest, ~{used_s} токенов). "
        "Полные отчёты сохранены в GCS без изменений."
        "</div>"
    )


def build_weekly_email_html(
    *,
    period_label: str,
    analysis_result: dict[str, Any],
    model: str,
    context_prep: dict[str, Any],
    archives_meta: dict[str, Any],
) -> str:
    """HTML недельного отчёта для email и outcomes."""
    ctx_mode = context_prep.get("context_mode") or "full"
    meta_bits = [
        f"период: {html.escape(period_label)}",
        f"модель: {html.escape(model)}",
        f"архивов Garmin: {archives_meta.get('garmin_days_found', '?')}",
        f"контекст: {'полный' if ctx_mode == 'full' else 'FACT-digest'}",
    ]
    if context_prep.get("est_tokens") is not None:
        meta_bits.append(f"~{context_prep['est_tokens']:,} токенов входа".replace(",", " "))
    meta = " · ".join(meta_bits)

    disclaimer = weekly_context_disclaimer_html(context_prep)

    if analysis_result.get("ok") and analysis_result.get("analysis"):
        analysis_html = markdown_to_email_html(analysis_result["analysis"])
    else:
        err = html.escape(str(analysis_result.get("error", "unknown")))
        analysis_html = f'<p style="color:#a33;">Анализ недоступен: {err}</p>'

    missing = archives_meta.get("missing") or []
    missing_garmin = [m for m in missing if m.get("kind") == "garmin"]
    missing_note = ""
    if missing_garmin:
        days_str = ", ".join(html.escape(str(m.get("day", "?"))) for m in missing_garmin)
        missing_note = (
            f'<p style="color:#888;font-size:13px;">Нет Garmin-архива за: {days_str}.</p>'
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;background:#f5f6f8;">
  <div style="max-width:680px;margin:0 auto;padding:24px 20px;font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.55;">
    <h1 style="font-size:22px;margin:0 0 4px;color:#111;">MyBody — недельный отчёт</h1>
    <div style="color:#888;font-size:13px;margin-bottom:16px;">{meta}</div>
    {disclaimer}
    {missing_note}
    <h2 style="font-size:16px;margin:18px 0 10px;color:#374151;">Итоги недели и рекомендации</h2>
    {analysis_html}
    <div style="color:#aaa;font-size:12px;margin-top:20px;">Сформировано автоматически сервисом MyBody.</div>
  </div>
</body>
</html>"""


def _pipeline_failure_detail_rows(result: dict[str, Any], report_type: str) -> list[tuple[str, str]]:
    """Пары (метка, значение) для письма о сбое pipeline."""
    rows: list[tuple[str, str]] = []

    def add(label: str, key: str, *, alt: str | None = None) -> None:
        val = result.get(key) if alt is None else result.get(alt)
        if val is not None and str(val).strip():
            rows.append((label, str(val).strip()))

    add("Ошибка", "error")
    add("Детали", "detail")
    add("Подсказка", "hint")
    add("Ошибка email", "email_error")
    add("Детали SMTP", "email_detail")

    if result.get("gemini_daily_quota"):
        rows.append(("Квота Gemini", "исчерпан дневной лимит free tier"))
    add("Gemini", "gemini_hint")

    if report_type == "daily":
        add("Garmin archive", "detailed_analysis_error")
        add("Питание", "food_analysis_error")
        if result.get("food_analysis_skipped"):
            rows.append(("Питание пропущено", str(result["food_analysis_skipped"])))
        add("Итоговый отчёт", "combined_analysis_error")
        if result.get("partial"):
            rows.append(("Частичный успех", "да — отчёт в письме не отправлен"))
        storage = result.get("storage_archive") or {}
        if isinstance(storage, dict) and storage.get("ok") is False:
            rows.append(("GCS archive", str(storage.get("error", "upload failed"))))
        outcomes = result.get("storage_outcomes") or {}
        if isinstance(outcomes, dict) and outcomes.get("ok") is False:
            rows.append(("GCS outcomes", str(outcomes.get("error", "upload failed"))))
    else:
        add("Weekly анализ", "weekly_analysis_error")
        archives = result.get("archives") or {}
        if isinstance(archives, dict):
            gf = archives.get("garmin_days_found")
            ff = archives.get("food_days_found")
            if gf is not None:
                rows.append(("Архивов Garmin", str(gf)))
            if ff is not None:
                rows.append(("Архивов food", str(ff)))
        if result.get("context_mode"):
            rows.append(("Режим контекста", str(result["context_mode"])))

    models = result.get("gemini_models_tried")
    if isinstance(models, dict) and models:
        for step, tried in models.items():
            if tried:
                rows.append((f"Модели ({step})", ", ".join(str(m) for m in tried)))

    return rows


def build_pipeline_failure_text(
    *,
    report_type: str,
    period_label: str,
    result: dict[str, Any],
    logs_url: str | None = None,
) -> str:
    title = "дневного" if report_type == "daily" else "недельного"
    lines = [
        f"MyBody — сбой {title} отчёта",
        f"Период: {period_label}",
        "",
    ]
    for label, value in _pipeline_failure_detail_rows(result, report_type):
        lines.append(f"{label}: {value}")
    if logs_url:
        lines.extend(["", f"Логи Cloud Run: {logs_url}"])
    lines.append("")
    lines.append("Проверьте Cloud Run Logs и повторите запуск вручную при необходимости.")
    return "\n".join(lines)


def build_pipeline_failure_email_html(
    *,
    report_type: str,
    period_label: str,
    result: dict[str, Any],
    logs_url: str | None = None,
) -> str:
    title_adj = "дневного" if report_type == "daily" else "недельного"
    td = 'style="border:1px solid #e8eaed;padding:8px 10px;font-size:14px;vertical-align:top;"'

    detail_rows = _pipeline_failure_detail_rows(result, report_type)
    if detail_rows:
        body_rows = "".join(
            f"<tr><td {td}><strong>{html.escape(label)}</strong></td>"
            f"<td {td}>{html.escape(value)}</td></tr>"
            for label, value in detail_rows
        )
        details_table = (
            f'<table style="border-collapse:collapse;width:100%;margin:12px 0;">'
            f"<tbody>{body_rows}</tbody></table>"
        )
    else:
        details_table = '<p style="color:#666;">Подробности смотрите в логах Cloud Run.</p>'

    quota_banner = ""
    if result.get("gemini_daily_quota"):
        quota_banner = (
            '<div style="background:#fde8e8;border:1px solid #e8a0a0;padding:12px 14px;'
            'margin:0 0 16px;border-radius:6px;font-size:14px;color:#7a1f1f;">'
            "<strong>Исчерпана дневная квота Gemini (free tier).</strong> "
            "Дождитесь сброса лимита (UTC) или задайте другие модели в env."
            "</div>"
        )

    logs_block = ""
    if logs_url:
        safe_url = html.escape(logs_url)
        logs_block = (
            f'<p style="margin:16px 0 8px;">'
            f'<a href="{safe_url}" style="color:#1a56db;">Открыть логи Cloud Run</a>'
            f"</p>"
        )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;background:#f5f6f8;">
  <div style="max-width:680px;margin:0 auto;padding:24px 20px;font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.55;">
    <h1 style="font-size:22px;margin:0 0 4px;color:#a33;">MyBody — сбой {html.escape(title_adj)} отчёта</h1>
    <div style="color:#888;font-size:13px;margin-bottom:16px;">период: {html.escape(period_label)}</div>
    {quota_banner}
    <p style="font-size:15px;margin:0 0 8px;">
      Обычное письмо с отчётом <strong>не отправлено</strong>. Краткая диагностика ниже.
    </p>
    {details_table}
    {logs_block}
    <div style="color:#aaa;font-size:12px;margin-top:20px;">Автоматическое уведомление MyBody (pipeline failure alert).</div>
  </div>
</body>
</html>"""


_BJU_LINE_RE = re.compile(
    r"БЖУ:\s*kcal\s+([\d.]+)\s*\|\s*белок\s+([\d.]+)\s*г\s*\|\s*жир\s+([\d.]+)\s*г\s*"
    r"\|\s*углев\s+([\d.]+)\s*г\s*\|\s*клетчатка\s+([\d.]+)\s*г",
    re.I,
)
_DAILY_TOTALS_BLOCK_RE = re.compile(r"## daily_totals\b([\s\S]*?)(?=^## |\Z)", re.I | re.M)
_DAILY_KCAL_RE = re.compile(r"\*\*Калории:\*\*\s*~?\s*([\d.]+)", re.I)
_DAILY_PROTEIN_RE = re.compile(r"\*\*Белки:\*\*\s*~?\s*([\d.]+)", re.I)


def validate_food_arithmetic(analysis: str) -> dict[str, Any]:
    """
    Сверяет строки БЖУ из meals_breakdown с daily_totals.
    Возвращает sums, reported, issues — для предупреждений в архиве и email.
    """
    issues: list[str] = []
    rows: list[tuple[float, float, float, float, float]] = []
    for m in _BJU_LINE_RE.finditer(analysis):
        rows.append(tuple(float(m.group(i)) for i in range(1, 6)))

    if not rows:
        if "## arithmetic_check" not in analysis.lower():
            issues.append("нет секции arithmetic_check и строк «БЖУ: kcal …» — суммы могут быть неточны")
        return {"ok": False, "issues": issues, "bju_rows": 0}

    sums = {
        "kcal": round(sum(r[0] for r in rows), 1),
        "protein_g": round(sum(r[1] for r in rows), 1),
        "fat_g": round(sum(r[2] for r in rows), 1),
        "carbs_g": round(sum(r[3] for r in rows), 1),
        "fiber_g": round(sum(r[4] for r in rows), 1),
    }
    reported: dict[str, float] = {}
    dt = _DAILY_TOTALS_BLOCK_RE.search(analysis)
    if dt:
        block = dt.group(1)
        km = _DAILY_KCAL_RE.search(block)
        pm = _DAILY_PROTEIN_RE.search(block)
        if km:
            reported["kcal"] = float(km.group(1))
        if pm:
            reported["protein_g"] = float(pm.group(1))

    def _pct_diff(reported_val: float, sum_val: float) -> float:
        if sum_val <= 0:
            return 100.0
        return abs(reported_val - sum_val) / sum_val * 100.0

    for key, label, tol in (
        ("kcal", "калории", 5.0),
        ("protein_g", "белок", 8.0),
    ):
        if key in reported and _pct_diff(reported[key], sums[key]) > tol:
            issues.append(
                f"daily_totals ({label} {reported[key]}) расходится с суммой БЖУ ({sums[key]}) "
                f"более чем на {tol:.0f}%"
            )

    return {
        "ok": not issues,
        "issues": issues,
        "bju_rows": len(rows),
        "sums": sums,
        "reported": reported,
    }


def food_arithmetic_warning(analysis: str) -> str | None:
    """Краткое предупреждение для вставки в отчёт, если арифметика не сходится."""
    v = validate_food_arithmetic(analysis)
    if v.get("ok"):
        return None
    parts = ["⚠ **Автопроверка арифметики:**"]
    for issue in v.get("issues") or []:
        parts.append(f"- {issue}")
    if v.get("sums"):
        s = v["sums"]
        parts.append(
            f"- Сумма по строкам БЖУ: {s['kcal']} kcal, белок {s['protein_g']} г "
            f"(используйте эти цифры, если daily_totals расходится)."
        )
    return "\n".join(parts)


_SECTION_TITLES: dict[str, str] = {
    "executive_summary": "Краткий итог",
    "garmin_key_points": "Garmin — главное",
    "nutrition_key_points": "Питание — главное",
    "cross_domain_insights": "Связи Garmin и питания",
    "unified_recommendations": "Рекомендации на завтра",
    "watch_out": "На что обратить внимание",
    "tomorrow_focus": "Фокус на завтра",
}

_SECTION_ACCENT: dict[str, str] = {
    "watch_out": "warn",
    "tomorrow_focus": "good",
    "executive_summary": "neutral",
}

_WARN_RE = re.compile(
    r"severity.*\bhigh\b|\bpriority.*\bhigh\b|\bhigh\b.*(?:severity|priority|риск)|"
    r"\b(?:риск|дефицит|избыт|недосып|перегруз|опасн|harmful|elevated|повышен|"
    r"высок(?:ий|ом|ая|ое)?\s+стресс|potentially\s+harmful|мало\s+(?:deep|rem|клетчатки)|"
    r"недобор|перебор|избыточн|критич|⚠|❌|"
    r"\bFAIR\b|\bPOOR\b|удовлетворительн|ниже\s+оптимальн|ниже\s+норм|"
    r"неплох(?:ой|им|ая|ое)?(?=\s|,|\.)|mixed|зона\s+внимания)",
    re.I,
)
_BAD_RE = re.compile(
    r"\b(?:bad|critical|severe|критич|опасн|very\s+poor)\b|severity.*\bhigh\b",
    re.I,
)
_GOOD_RE = re.compile(
    r"severity.*\blow\b|\bpriority.*\blow\b|\b(?:supportive|balanced)\b|"
    r"\b(?:хорош(?:о|ий|ая|ее)?|отличн|достаточн|в\s+норме|положител|"
    r"восстановлен(?:о|ы)?|сбалансир|оптимальн|нормальн|excellent|\bGOOD\b|✅|👍)",
    re.I,
)

_TONE_IN_BOLD_RE = re.compile(
    r"^\[(good|warn|neutral|bad)\]\s*\*\*(.+?):\*\*\s*(.*)$",
    re.I | re.S,
)
_BOLD_TONE_RE = re.compile(
    r"^\*\*\[(good|warn|neutral|bad)\]\s*(.+?):\*\*\s*(.*)$",
    re.I | re.S,
)
_BOLD_LABEL_RE = re.compile(r"^\*\*(.+?):\*\*\s*(.*)$", re.I | re.S)
_PRIORITY_TAG_RE = re.compile(r"\[priority:\s*(high|medium|low)\]", re.I)
_LEADING_TONE_TAG_RE = re.compile(r"^\[(good|warn|neutral|bad)\]\s*", re.I)


def _strip_leading_tone_tags(text: str) -> tuple[str, str]:
    tone = "neutral"
    s = text.strip()
    while True:
        m = _LEADING_TONE_TAG_RE.match(s)
        if not m:
            break
        tone = m.group(1).lower()
        s = s[m.end() :].strip()
    return tone, s


_CIRCLE_ICONS = frozenset("🔴🟡🟢")


def _recommendation_icon_html(icon: str) -> str:
    if icon in _CIRCLE_ICONS:
        size = "11px"
    else:
        size = "15px"
    return (
        f'<span style="font-size:{size};line-height:1;vertical-align:0.08em;'
        f'margin-right:7px;display:inline-block;" aria-hidden="true">{icon}</span>'
    )


def _recommendation_icon(priority: str | None, tone: str) -> str:
    if priority == "high":
        return "🔴" if tone in ("warn", "bad") else "❗"
    if priority == "medium":
        return "🟡"
    if priority == "low":
        return "🟢"
    if tone == "bad":
        return "🚨"
    if tone == "warn":
        return "⚠️"
    if tone == "good":
        return "✅"
    return "💡"


def _recommendation_border(priority: str | None, tone: str) -> str:
    if priority == "high" or tone == "bad":
        return "#ef4444"
    if priority == "medium" or tone == "warn":
        return "#f59e0b"
    if priority == "low" or tone == "good":
        return "#22c55e"
    return "#94a3b8"


def _parse_unified_recommendation(content: str) -> tuple[str | None, str, str | None, str]:
    s = content.strip()
    priority: str | None = None
    pm = _PRIORITY_TAG_RE.search(s)
    if pm:
        priority = pm.group(1).lower()
        s = _PRIORITY_TAG_RE.sub("", s).strip()
    tone, s = _strip_leading_tone_tags(s)
    parsed_tone, label, body = _parse_labeled_line(s, "neutral")
    if parsed_tone != "neutral":
        tone = parsed_tone
    return priority, tone, label, body


def _render_recommendation_line_html(content: str) -> str:
    priority, tone, label, body = _parse_unified_recommendation(content)
    icon = _recommendation_icon(priority, tone)
    border = _recommendation_border(priority, tone)
    if label:
        text_html = (
            f'<strong style="color:#111;">{html.escape(label)}</strong>'
            + (f": {_inline_md(body)}" if body else "")
        )
    else:
        fallback = body or content.strip()
        text_html = _inline_md(fallback)
    icon_html = _recommendation_icon_html(icon)
    return (
        f'<li style="margin:0 0 10px;'
        f"padding:12px 14px;background:#fafafa;border-radius:8px;border-left:3px solid {border};"
        f'list-style:none;">'
        f'<div style="color:#333;line-height:1.55;font-size:14px;">'
        f"{icon_html}{text_html}"
        f"</div></li>"
    )


def _render_emoji_tagged_body(lines: list[str]) -> str:
    """Список или абзац с [priority]/[tone] → карточки с эмодзи (рекомендации, фокус на завтра)."""
    items: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        ul = re.match(r"^[\*\-]\s+(.*)$", line)
        ol = re.match(r"^\d+\.\s+(.*)$", line)
        payload = (ul or ol).group(1).strip() if (ul or ol) else line  # type: ignore[union-attr]
        items.append(_render_recommendation_line_html(payload))
    if not items:
        return ""
    return f'<ul style="margin:0;padding:0;">{"".join(items)}</ul>'


def _inline_md(text: str) -> str:
    s = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)


def _line_tone_heuristic(text: str, section_slug: str) -> str:
    accent = _SECTION_ACCENT.get(section_slug)
    if accent == "warn":
        return "warn"
    if accent == "good":
        return "good"
    if _BAD_RE.search(text):
        return "bad"
    if _WARN_RE.search(text):
        return "warn"
    if _GOOD_RE.search(text):
        return "good"
    return "neutral"


def _plain_label_style() -> str:
    return "color:#000;font-weight:bold;"


def _label_style(tone: str, *, section_slug: str) -> str:
    if section_slug == "executive_summary":
        return _plain_label_style()
    if tone == "good":
        return "color:#166534;background:#ecfdf5;padding:2px 8px;border-radius:4px;font-weight:bold;"
    if tone == "warn":
        return "color:#9a3412;background:#fff7ed;padding:2px 8px;border-radius:4px;font-weight:bold;"
    if tone == "bad":
        return "color:#991b1b;background:#fee2e2;padding:2px 8px;border-radius:4px;font-weight:bold;"
    return "color:#374151;background:#f3f4f6;padding:2px 8px;border-radius:4px;font-weight:bold;"


def _parse_labeled_line(content: str, section_slug: str) -> tuple[str, str | None, str]:
    """(tone, label_or_none, body) — явный тег Gemini или эвристика только для метки."""
    s = content.strip()

    for pat in (_TONE_IN_BOLD_RE, _BOLD_TONE_RE):
        m = pat.match(s)
        if m:
            tone = m.group(1).lower()
            label = _LEADING_TONE_TAG_RE.sub("", m.group(2).strip()).strip()
            return tone, label, _clean_line_body(m.group(3).strip())

    m = _BOLD_LABEL_RE.match(s)
    if m:
        label = _LEADING_TONE_TAG_RE.sub("", m.group(1).strip()).strip()
        body = m.group(2).strip()
        tone = _line_tone_heuristic(f"{label} {body[:120]}", section_slug)
        if tone == "good" and _WARN_RE.search(body):
            tone = "warn"
        return tone, label, _clean_line_body(body)

    leading_tone, rest = _strip_leading_tone_tags(s)
    if ":" in rest:
        label, _, body = rest.partition(":")
        label = re.sub(r"^\*+|\*+$", "", label.strip()).strip()
        label = _LEADING_TONE_TAG_RE.sub("", label).strip()
        body = body.strip()
        tone = leading_tone
        if tone == "neutral":
            tone = _line_tone_heuristic(f"{label} {body[:120]}", section_slug)
        if label:
            return tone, label, _clean_line_body(body)

    return _line_tone_heuristic(s, section_slug), None, _clean_line_body(s)


def _clean_line_body(body: str) -> str:
    """Убирает теги [good/warn/bad/neutral] из текста после метки."""
    if not body:
        return body
    _, body = _strip_leading_tone_tags(body.strip())
    body = re.sub(r"\s*[—–-]\s*\[(good|warn|neutral|bad)\]\s*", " — ", body, flags=re.I)
    body = re.sub(r"^\[(good|warn|neutral|bad)\]\s*", "", body, flags=re.I)
    body = re.sub(r"\s*[—–-]\s*\[(good|warn|neutral|bad)\]\s*$", "", body, flags=re.I)
    return re.sub(r"\s+\[(good|warn|neutral|bad)\]\s*$", "", body, flags=re.I).strip()


def _render_line_html(content: str, section_slug: str, *, as_list_item: bool) -> str:
    tone, label, body = _parse_labeled_line(content, section_slug)
    tag = "li" if as_list_item else "p"
    text_color = "#000" if section_slug == "executive_summary" else "#333"
    base = f"margin:8px 0;padding:0;color:{text_color};line-height:1.55;list-style:none;"

    if label is not None:
        label_html = (
            f'<span style="{_label_style(tone, section_slug=section_slug)}">{html.escape(label)}</span>'
        )
        body_html = _inline_md(body) if body else ""
        inner = f"{label_html}: {body_html}" if body_html else f"{label_html}:"
    else:
        inner = _inline_md(body or content)

    return f"<{tag} style=\"{base}\">{inner}</{tag}>"


def _section_header_style(accent: str, *, slug: str) -> str:
    if slug == "executive_summary":
        return "background:#fff;color:#000;border-bottom:1px solid #e3e5e8;"
    if accent == "warn":
        return "background:#fee2e2;color:#991b1b;border-bottom:1px solid #fecaca;"
    if accent == "good":
        return "background:#dcfce7;color:#166534;border-bottom:1px solid #bbf7d0;"
    return "background:#eef2ff;color:#1e3a5f;border-bottom:1px solid #dbeafe;"


def _render_body_lines(lines: list[str], section_slug: str) -> str:
    parts: list[str] = []
    list_buf: list[str] = []

    def flush_list() -> None:
        nonlocal list_buf
        if not list_buf:
            return
        items = "".join(_render_line_html(item, section_slug, as_list_item=True) for item in list_buf)
        parts.append(f'<ul style="margin:0;padding:0;">{items}</ul>')
        list_buf = []

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_list()
            continue
        h3 = re.match(r"^###\s+(.*)$", line)
        if h3:
            flush_list()
            parts.append(
                f'<div style="margin:14px 0 6px;font-size:14px;font-weight:bold;color:#374151;">'
                f"{_inline_md(h3.group(1))}</div>"
            )
            continue
        ul = re.match(r"^[\*\-]\s+(.*)$", line)
        ol = re.match(r"^\d+\.\s+(.*)$", line)
        if ul or ol:
            list_buf.append((ul or ol).group(1))  # type: ignore[union-attr]
            continue
        flush_list()
        parts.append(_render_line_html(line, section_slug, as_list_item=False))

    flush_list()
    return "\n".join(parts)


def _render_section(slug: str, body: str) -> str:
    title = _SECTION_TITLES.get(slug, slug.replace("_", " ").strip().capitalize())
    accent = _SECTION_ACCENT.get(slug, "neutral")
    if slug == "watch_out":
        accent = "warn"
    elif slug == "tomorrow_focus":
        accent = "good"

    if slug in ("unified_recommendations", "tomorrow_focus"):
        body_html = _render_emoji_tagged_body(body.split("\n"))
    else:
        body_html = _render_body_lines(body.split("\n"), slug)
    if slug == "executive_summary":
        border = "#e3e5e8"
        header_style = _section_header_style(accent, slug=slug)
    else:
        border = "#fecaca" if accent == "warn" else "#bbf7d0" if accent == "good" else "#e3e5e8"
        header_style = _section_header_style(accent, slug=slug)
    return f"""
<div style="margin:0 0 14px;border:1px solid {border};border-radius:10px;overflow:hidden;">
  <div style="padding:10px 14px;font-size:15px;font-weight:bold;{header_style}">
    {html.escape(title)}
  </div>
  <div style="padding:12px 14px 14px;background:#fff;font-size:14px;line-height:1.55;color:#000;">
    {body_html}
  </div>
</div>"""


def markdown_to_email_html(text: str) -> str:
    """
    Markdown отчёта Gemini → HTML для email/outcomes.
    ## секции → карточки; [good]/[warn]/[bad] на метке — цвет только заголовка пункта.
    """
    raw = text.strip()
    if not raw:
        return ""

    if not re.search(r"^##\s+", raw, flags=re.MULTILINE):
        return _render_body_lines(raw.split("\n"), "neutral")

    chunks = re.split(r"^##\s+", raw, flags=re.MULTILINE)
    parts: list[str] = []

    preamble = chunks[0].strip()
    if preamble:
        parts.append(
            f'<div style="margin:0 0 14px;padding:12px 14px;background:#fff;'
            f'border:1px solid #e3e5e8;border-radius:10px;">'
            f"{_render_body_lines(preamble.split('\n'), 'neutral')}</div>"
        )

    for chunk in chunks[1:]:
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.split("\n")
        slug = lines[0].strip().split()[0]
        body = "\n".join(lines[1:]).strip()
        parts.append(_render_section(slug, body))

    return "\n".join(parts)
