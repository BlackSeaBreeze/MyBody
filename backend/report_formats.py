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


def _label_style(tone: str) -> str:
    if tone == "good":
        return "color:#166534;background:#ecfdf5;padding:2px 8px;border-radius:4px;font-weight:bold;"
    if tone == "warn":
        return "color:#9a3412;background:#fff7ed;padding:2px 8px;border-radius:4px;font-weight:bold;"
    if tone == "bad":
        return "color:#991b1b;background:#fee2e2;padding:2px 8px;border-radius:4px;font-weight:bold;"
    return "color:#374151;font-weight:bold;"


def _parse_labeled_line(content: str, section_slug: str) -> tuple[str, str | None, str]:
    """(tone, label_or_none, body) — явный тег Gemini или эвристика только для метки."""
    for pat in (_TONE_IN_BOLD_RE, _BOLD_TONE_RE):
        m = pat.match(content.strip())
        if m:
            return m.group(1).lower(), m.group(2).strip(), m.group(3).strip()

    m = _BOLD_LABEL_RE.match(content.strip())
    if m:
        label, body = m.group(1).strip(), m.group(2).strip()
        tone = _line_tone_heuristic(f"{label} {body[:120]}", section_slug)
        if tone == "good" and _WARN_RE.search(body):
            tone = "warn"
        return tone, label, body

    return _line_tone_heuristic(content, section_slug), None, content.strip()


def _render_line_html(content: str, section_slug: str, *, as_list_item: bool) -> str:
    tone, label, body = _parse_labeled_line(content, section_slug)
    tag = "li" if as_list_item else "p"
    base = "margin:8px 0;padding:0;color:#333;line-height:1.55;list-style:none;"

    if label is not None:
        label_html = (
            f'<span style="{_label_style(tone)}">{html.escape(label)}</span>'
        )
        body_html = _inline_md(body) if body else ""
        inner = f"{label_html}: {body_html}" if body_html else f"{label_html}:"
    else:
        inner = _inline_md(content)
        if tone in ("good", "warn", "bad"):
            inner = f'<span style="{_label_style(tone)}">{inner}</span>'

    return f"<{tag} style=\"{base}\">{inner}</{tag}>"


def _section_header_style(accent: str) -> str:
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

    body_html = _render_body_lines(body.split("\n"), slug)
    border = "#fecaca" if accent == "warn" else "#bbf7d0" if accent == "good" else "#e3e5e8"
    return f"""
<div style="margin:0 0 14px;border:1px solid {border};border-radius:10px;overflow:hidden;">
  <div style="padding:10px 14px;font-size:15px;font-weight:bold;{_section_header_style(accent)}">
    {html.escape(title)}
  </div>
  <div style="padding:12px 14px 14px;background:#fff;font-size:14px;line-height:1.55;">
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
