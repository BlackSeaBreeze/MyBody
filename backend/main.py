"""
MyBody — бэкенд: ежедневные рекомендации и чат-агент на Gemini.
Данные из приложения (фото еды, витамины, активность, Garmin) — один контекст для отчёта и чата.
"""
import html
import logging
import os
import re
import time
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from backend import drive_client, email_client, garmin_client, gemini_client, storage_client
from backend import report_formats
from backend.gemini_client import DEFAULT_GEMINI_MODEL

logger = logging.getLogger(__name__)


def _gemini_inter_call_delay_sec() -> int:
    """Пауза между двумя вызовами Gemini (TPM сбрасывается поминутно на Free tier)."""
    try:
        return max(0, int(os.environ.get("GEMINI_INTER_CALL_DELAY_SEC", "65")))
    except ValueError:
        return 65


app = FastAPI(
    title="MyBody API",
    description="Здоровье, питание, сон — рекомендации и чат с агентом",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"service": "MyBody", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Пустой ответ для favicon — браузеры запрашивают его автоматически."""
    return Response(status_code=204)


# --- Garmin ---

@app.get("/garmin/status")
def garmin_status():
    """Проверка: заданы ли учётные данные Garmin (без попытки логина)."""
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    configured = bool(email and os.environ.get("GARMIN_PASSWORD", "").strip())
    return {"garmin_configured": configured}


@app.get("/garmin/data")
def garmin_data(days: int = 7):
    """
    Возвращает данные из Garmin Connect за последние days дней (по умолчанию 7).
    Удобно открыть в браузере и посмотреть, что приходит: активности и статистика по дням.
    """
    data = garmin_client.fetch_recent_data(days=min(max(1, days), 31))
    return data


@app.get("/garmin/metrics")
def garmin_metrics(days: int = 7):
    """
    Все доступные метрики Garmin за последние days дней: stats, sleep, heart_rates,
    stress, body_battery, hydration, respiration, SpO2, HRV, training_readiness и др.
    Структура metrics_by_day[date] готова для передачи в Gemini как контекст.
    """
    data = garmin_client.fetch_all_metrics(days=min(max(1, days), 31))
    return data


@app.get("/garmin/summary")
def garmin_summary(days: int = 7):
    """Удобочитаемая сводка по дням: шаги, сон, калории, стресс, Body Battery, пульс."""
    raw = garmin_client.fetch_recent_data(days=min(max(1, days), 31))
    return garmin_client.build_readable_summary(raw)


@app.get("/garmin/view", response_class=HTMLResponse)
def garmin_view(days: int = 7):
    """Страница с таблицей: данные Garmin по дням в удобочитаемом виде."""
    raw = garmin_client.fetch_recent_data(days=min(max(1, days), 31))
    summary = garmin_client.build_readable_summary(raw)
    if not summary.get("ok") or not summary.get("days"):
        return _garmin_view_html(
            error=summary.get("error", "Нет данных или Garmin не настроен"),
            days_list=[],
            period=None,
        )
    return _garmin_view_html(
        error=None,
        days_list=summary["days"],
        period=(summary.get("from"), summary.get("to")),
    )


def _garmin_view_html(
    *,
    error: str | None,
    days_list: list[dict],
    period: tuple[str | None, str | None] | None,
) -> str:
    period_str = f"{period[0]} — {period[1]}" if period and period[0] and period[1] else ""
    rows = ""
    for d in days_list:
        steps = d.get("steps") or "—"
        sleep = d.get("sleep") or "—"
        cal = d.get("calories_total")
        cal_str = str(cal) if cal is not None else "—"
        stress = d.get("stress") or "—"
        bb = d.get("body_battery") or "—"
        hr = d.get("resting_hr")
        hr_str = str(hr) if hr is not None else "—"
        dist = d.get("distance_km")
        dist_str = f"{dist} км" if dist is not None else "—"
        rows += f"""
        <tr>
            <td><strong>{d.get('date', '')}</strong></td>
            <td>{steps}</td>
            <td>{sleep}</td>
            <td>{cal_str}</td>
            <td>{dist_str}</td>
            <td>{stress}</td>
            <td>{bb}</td>
            <td>{hr_str}</td>
        </tr>"""
    table = f"""
    <table>
        <thead>
            <tr>
                <th>Дата</th>
                <th>Шаги / цель</th>
                <th>Сон</th>
                <th>Ккал</th>
                <th>Расстояние</th>
                <th>Стресс</th>
                <th>Body Battery</th>
                <th>Пульс покоя</th>
            </tr>
        </thead>
        <tbody>{rows}
        </tbody>
    </table>""" if days_list else "<p>Нет данных за период.</p>"
    err_block = f'<p class="error">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>MyBody — Garmin</title>
    <style>
        body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #1a1a1a; color: #e0e0e0; }}
        h1 {{ font-size: 1.5rem; }}
        .period {{ color: #888; margin-bottom: 1rem; }}
        .error {{ color: #e88; }}
        table {{ border-collapse: collapse; width: 100%; max-width: 900px; }}
        th, td {{ border: 1px solid #444; padding: 0.5rem 0.75rem; text-align: left; }}
        th {{ background: #333; }}
        tr:nth-child(even) {{ background: #252525; }}
    </style>
</head>
<body>
    <h1>Данные Garmin</h1>
    <p class="period">{period_str}</p>
    {err_block}
    {table}
</body>
</html>"""


@app.get("/garmin/analyze")
def garmin_analyze(
    request: Request,
    days: int = 7,
    model: str | None = None,
    format: str | None = None,
):
    """
    Загружает полные метрики Garmin за последние days дней, отправляет их в Gemini
    и возвращает текстовый анализ и рекомендации по здоровью и активности.
    Требуется переменная окружения GEMINI_API_KEY (ключ из Google AI Studio).

    По умолчанию в браузере отдаётся удобочитаемая HTML-страница, программным
    клиентам (Accept: application/json) — JSON. Принудительно: ?format=html|json.
    """
    days = min(max(1, days), 31)
    model = (model or DEFAULT_GEMINI_MODEL).strip()
    metrics = garmin_client.fetch_all_metrics(days=days)
    result = gemini_client.analyze_garmin_metrics(metrics, model=model)

    if format == "json":
        want_html = False
    elif format == "html":
        want_html = True
    else:
        want_html = "text/html" in (request.headers.get("accept") or "")

    if want_html:
        return HTMLResponse(_analyze_html(result, days=days, model=model))
    return JSONResponse(result)


def _markdown_to_html(text: str) -> str:
    """Минимальная конвертация markdown-вывода модели в безопасный HTML."""
    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return s

    html_parts: list[str] = []
    list_buffer: list[str] = []
    list_type: str | None = None  # "ul" | "ol"

    def flush_list() -> None:
        nonlocal list_buffer, list_type
        if list_buffer and list_type:
            items = "".join(f"<li>{it}</li>" for it in list_buffer)
            html_parts.append(f"<{list_type}>{items}</{list_type}>")
        list_buffer = []
        list_type = None

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            flush_list()
            continue
        ul = re.match(r"^[\*\-]\s+(.*)$", line)
        ol = re.match(r"^\d+\.\s+(.*)$", line)
        if ul:
            if list_type != "ul":
                flush_list()
                list_type = "ul"
            list_buffer.append(inline(ul.group(1)))
        elif ol:
            if list_type != "ol":
                flush_list()
                list_type = "ol"
            list_buffer.append(inline(ol.group(1)))
        else:
            flush_list()
            html_parts.append(f"<p>{inline(line)}</p>")
    flush_list()
    return "\n".join(html_parts)


def _analyze_html(result: dict, *, days: int, model: str) -> str:
    ctx = result.get("context") or {}
    meta_bits = []
    if ctx:
        meta_bits.append(f"ужатие: уровень {ctx.get('compaction_level')}")
        if ctx.get("est_tokens") is not None:
            meta_bits.append(f"~{ctx.get('est_tokens'):,} токенов".replace(",", " "))
    meta_bits.append(f"модель: {html.escape(model)}")
    meta_bits.append(f"период: {days} дн.")
    meta_str = " · ".join(meta_bits)

    if result.get("ok") and result.get("analysis"):
        body = _markdown_to_html(result["analysis"])
    else:
        err = html.escape(str(result.get("error", "unknown")))
        hint = result.get("hint")
        detail = result.get("detail")
        body = f'<p class="error">Ошибка: {err}</p>'
        if hint:
            body += f'<p class="hint">{html.escape(str(hint))}</p>'
        if detail:
            body += f'<p class="detail">{html.escape(str(detail))}</p>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>MyBody — анализ Garmin</title>
    <style>
        body {{ font-family: system-ui, sans-serif; margin: 0; background: #1a1a1a; color: #e0e0e0; line-height: 1.6; }}
        .wrap {{ max-width: 760px; margin: 0 auto; padding: 2rem 1.5rem 4rem; }}
        h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
        .meta {{ color: #888; font-size: 0.85rem; margin-bottom: 1.5rem; }}
        .card {{ background: #232323; border: 1px solid #383838; border-radius: 12px; padding: 1.25rem 1.5rem; }}
        .card p {{ margin: 0.6rem 0; }}
        .card ul, .card ol {{ margin: 0.4rem 0 0.9rem; padding-left: 1.4rem; }}
        .card li {{ margin: 0.3rem 0; }}
        strong {{ color: #fff; }}
        .error {{ color: #ff8a8a; }}
        .hint {{ color: #d8c98a; }}
        .detail {{ color: #888; font-size: 0.85rem; white-space: pre-wrap; }}
        a {{ color: #7aa2ff; }}
    </style>
</head>
<body>
    <div class="wrap">
        <h1>Анализ данных Garmin</h1>
        <div class="meta">{meta_str}</div>
        <div class="card">{body}</div>
    </div>
</body>
</html>"""


def _summary_table_html(summary: dict) -> str:
    """Компактная таблица показателей за день для письма (inline-стили — для почтовых клиентов)."""
    days = summary.get("days") or []
    if not days:
        return '<p style="color:#a33;">Нет данных для таблицы.</p>'
    th = 'style="border:1px solid #ddd;padding:6px 10px;text-align:left;background:#f3f4f6;font-size:13px;"'
    td = 'style="border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:13px;"'
    rows = ""
    for d in days:
        cal = d.get("calories_total")
        dist = d.get("distance_km")
        hr = d.get("resting_hr")
        rows += (
            "<tr>"
            f'<td {td}><strong>{html.escape(str(d.get("date", "")))}</strong></td>'
            f'<td {td}>{html.escape(str(d.get("steps") or "—"))}</td>'
            f'<td {td}>{html.escape(str(d.get("sleep") or "—"))}</td>'
            f'<td {td}>{cal if cal is not None else "—"}</td>'
            f'<td {td}>{(str(dist) + " км") if dist is not None else "—"}</td>'
            f'<td {td}>{html.escape(str(d.get("stress") or "—"))}</td>'
            f'<td {td}>{html.escape(str(d.get("body_battery") or "—"))}</td>'
            f'<td {td}>{hr if hr is not None else "—"}</td>'
            "</tr>"
        )
    return (
        '<table style="border-collapse:collapse;width:100%;margin:8px 0 16px;">'
        "<thead><tr>"
        f"<th {th}>Дата</th><th {th}>Шаги / цель</th><th {th}>Сон</th><th {th}>Ккал</th>"
        f"<th {th}>Расстояние</th><th {th}>Стресс</th><th {th}>Body Battery</th><th {th}>Пульс покоя</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _gemini_sleep() -> int:
    delay = _gemini_inter_call_delay_sec()
    if delay > 0:
        time.sleep(delay)
    return delay


def _daily_email_html(
    *,
    day_label: str,
    analysis_result: dict,
    summary: dict,
    model: str,
    has_food_attachment: bool = False,
) -> str:
    """Светлое email-оформление: таблица Garmin + итоговый согласованный анализ."""
    ctx = analysis_result.get("context") or {}
    meta_bits = [f"день: {html.escape(day_label)}", f"модель: {html.escape(model)}"]
    if ctx.get("has_food_analysis"):
        meta_bits.append("Garmin + питание")
    else:
        meta_bits.append("только Garmin")
    if ctx.get("est_tokens") is not None:
        meta_bits.append(f"~{ctx.get('est_tokens'):,} токенов Garmin".replace(",", " "))
    meta = " · ".join(meta_bits)

    if analysis_result.get("ok") and analysis_result.get("analysis"):
        analysis_html = report_formats.markdown_to_email_html(analysis_result["analysis"])
    else:
        err = html.escape(str(analysis_result.get("error", "unknown")))
        analysis_html = f'<p style="color:#a33;">Анализ недоступен: {err}</p>'

    table_html = _summary_table_html(summary)
    attachment_note = (
        '<p style="color:#888;font-size:13px;margin-top:14px;">'
        "Подробный анализ питания за день — во вложении (.md), тот же файл что в архиве GCS.</p>"
        if has_food_attachment
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;background:#f5f6f8;">
  <div style="max-width:680px;margin:0 auto;padding:24px 20px;font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.55;">
    <h1 style="font-size:22px;margin:0 0 4px;color:#111;">MyBody — дневной отчёт</h1>
    <div style="color:#888;font-size:13px;margin-bottom:20px;">{meta}</div>

    <h2 style="font-size:16px;margin:18px 0 8px;color:#374151;">Показатели Garmin за день</h2>
    {table_html}

    <h2 style="font-size:16px;margin:22px 0 10px;color:#374151;">Итоговый анализ и рекомендации</h2>
    {analysis_html}
    {attachment_note}

    <div style="color:#aaa;font-size:12px;margin-top:20px;">Сформировано автоматически сервисом MyBody.</div>
  </div>
</body>
</html>"""


def _note_gemini_failure(result: dict, step_result: dict | None, step: str) -> None:
    if not step_result or step_result.get("ok"):
        return
    err = str(step_result.get("error", ""))
    if step_result.get("error_kind") == "gemini_daily_quota_exhausted" or gemini_client.is_daily_quota_error(err):
        result["gemini_daily_quota"] = True
        result["gemini_hint"] = step_result.get("hint") or (
            "Исчерпан дневной лимит free tier Gemini. Подождите до следующего дня (UTC) "
            "или задайте GEMINI_MODEL_FOOD / GEMINI_MODEL_COMBINED на другие модели."
        )
    ctx = step_result.get("context") or {}
    if ctx.get("models_tried"):
        result.setdefault("gemini_models_tried", {})[step] = ctx["models_tried"]


def _daily_report_http_status(result: dict, *, send: bool, save_reports: bool) -> int:
    if not result.get("ok"):
        return 502
    if result.get("gemini_daily_quota"):
        return 200
    if send and result.get("email_error") and not result.get("emailed"):
        err = str(result.get("combined_analysis_error", ""))
        if gemini_client.is_retryable_gemini_error(err):
            return 503
        return 502
    if save_reports and result.get("storage_outcomes", {}).get("ok") is False:
        return 502
    return 200


def _profile_hint_from_metrics(metrics: dict) -> str | None:
    """Краткий профиль из Garmin для уточнения норм питания."""
    profile = (metrics.get("global_metrics") or {}).get("user_profile")
    if not isinstance(profile, dict):
        return None
    parts: list[str] = []
    for key in ("gender", "weight", "height", "birthDate", "activityLevel"):
        val = profile.get(key)
        if val is not None:
            parts.append(f"{key}={val}")
    return "; ".join(parts) if parts else None


def _save_food_analysis(
    *,
    day_label: str,
    file_stem: str,
    model: str | None,
    metrics: dict,
    result: dict,
) -> None:
    """Загружает фото еды из Drive, анализирует Gemini, сохраняет в GCS archive."""
    if not drive_client.is_meals_configured():
        result["meals_skipped"] = "drive_meals_not_configured"
        return

    meals = drive_client.fetch_day_meal_photos(day_label)
    result["meals_fetch"] = {
        "ok": meals.get("ok"),
        "folder_name": meals.get("folder_name"),
        "folder_found": meals.get("folder_found"),
        "photo_count": meals.get("photo_count", 0),
        "error": meals.get("error"),
    }
    if not meals.get("ok"):
        logger.error("Meals fetch failed: %s", meals.get("error"))
        return
    if not meals.get("folder_found"):
        result["food_analysis_skipped"] = "day_folder_not_found"
        return
    if not meals.get("photos"):
        result["food_analysis_skipped"] = "no_photos"
        return

    delay = _gemini_inter_call_delay_sec()
    if delay > 0:
        time.sleep(delay)

    food = gemini_client.analyze_food_photos(
        meals["photos"],
        day_label=day_label,
        model=model,
        profile_hint=_profile_hint_from_metrics(metrics),
    )
    result["food_analysis_ok"] = bool(food.get("ok"))
    _note_gemini_failure(result, food, "food")
    if not food.get("ok"):
        result["food_analysis_error"] = food.get("error")
        logger.error("Food Gemini analysis failed: %s", food.get("error"))
        return

    food_md = report_formats.build_food_report_md(
        day_label=day_label,
        food_analysis=food["analysis"],
        model=food.get("model") or gemini_client.model_for_step("food", model),
        photo_meta=meals,
        context_meta=food.get("context"),
    )
    food_name = f"{file_stem}-food.md"
    food_up = storage_client.upload_to_archive(food_name, food_md)
    result["storage_archive_food"] = food_up
    if food_up.get("ok"):
        logger.info("GCS archive food saved: %s", food_up.get("gs_uri"))
    else:
        logger.error("GCS archive food failed: %s", food_up)


@app.post("/internal/daily-report")
def daily_report(
    day: str | None = None,
    model: str | None = None,
    send: bool = True,
    save_reports: bool = True,
    save_drive: bool | None = None,
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
):
    """
    Ежедневный отчёт за один календарный день (по умолчанию сегодня в REPORT_TIMEZONE; сон = прошедшая ночь).

    Порядок:
    1) Garmin → один Gemini-вызов → archive/vb-….md (сырые метрики остаются в памяти)
    2) Фото еды → archive/vb-…-food.md
    3) Сырые Garmin + последний food-архив за день → итоговый Gemini → outcomes/vb-….html + email

    Вызов защищён: при заданном CRON_SECRET требуется заголовок X-Cron-Secret.
    """
    if save_drive is not None:
        save_reports = save_drive

    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    metrics = garmin_client.fetch_daily_metrics(day=day)
    day_label = str(metrics.get("to") or day or "сегодня")
    gemini_override = model.strip() if model else None
    file_stem = report_formats.drive_file_stem()
    result: dict = {
        "ok": bool(metrics.get("ok")),
        "day": day_label,
        "file_stem": file_stem,
        "model": gemini_override or gemini_client.DEFAULT_GEMINI_MODEL,
        "models": {
            "archive": gemini_client.model_for_step("archive", gemini_override),
            "food": gemini_client.model_for_step("food", gemini_override),
            "combined": gemini_client.model_for_step("combined", gemini_override),
        },
    }

    if not metrics.get("ok"):
        result["error"] = metrics.get("error")
        if metrics.get("detail"):
            result["detail"] = metrics["detail"]
        if metrics.get("hint"):
            result["hint"] = metrics["hint"]
        return JSONResponse(result, status_code=502)

    summary = garmin_client.summary_from_metrics(metrics)
    html_body = ""
    combined: dict = {"ok": False, "error": "not_run"}
    food_archive: dict[str, Any] = {}

    if save_reports and storage_client.is_configured():
        # 1) Garmin — только архивный экспертный анализ (один вызов Gemini)
        detailed = gemini_client.analyze_garmin_metrics_detailed(metrics, model=gemini_override)
        result["detailed_analysis_ok"] = bool(detailed.get("ok"))
        _note_gemini_failure(result, detailed, "archive")
        if detailed.get("ok"):
            detailed_md = report_formats.build_detailed_report_md(
                day_label=day_label,
                detailed_analysis=detailed["analysis"],
                metrics=metrics,
                summary=summary,
                model=detailed.get("model") or result["models"]["archive"],
                context_meta=detailed.get("context"),
            )
            detailed_up = storage_client.upload_to_archive(f"{file_stem}.md", detailed_md)
            result["storage_archive"] = detailed_up
            if detailed_up.get("ok"):
                logger.info("GCS archive saved: %s", detailed_up.get("gs_uri"))
            else:
                logger.error("GCS archive failed: %s", detailed_up)
        else:
            result["detailed_analysis_error"] = detailed.get("error")
            result["storage_archive"] = {"ok": False, "error": "detailed_analysis_failed"}
            logger.error("Detailed Garmin analysis failed: %s", detailed.get("error"))

        result["gemini_inter_call_delay_sec"] = _gemini_sleep()

        # 2) Еда → archive/vb-…-food.md
        _save_food_analysis(
            day_label=day_label,
            file_stem=file_stem,
            model=gemini_override,
            metrics=metrics,
            result=result,
        )

        result["gemini_inter_call_delay_sec"] = _gemini_sleep()

        # 3) Последний food-архив за день + сырые Garmin → итоговый отчёт
        food_archive = storage_client.get_latest_food_analysis_for_day(day_label)
        food_analysis_text: str | None = None
        if food_archive.get("ok"):
            food_analysis_text = report_formats.extract_analysis_section(food_archive["content"])
            result["food_archive_used"] = {
                "object": food_archive.get("object"),
                "gs_uri": food_archive.get("gs_uri"),
                "candidates_count": food_archive.get("candidates_count"),
            }
        else:
            result["food_archive_used"] = {
                "ok": False,
                "error": food_archive.get("error"),
            }
            logger.warning("No food archive for combined report: %s", food_archive.get("error"))

        combined = gemini_client.analyze_daily_combined(
            metrics,
            food_analysis=food_analysis_text,
            day_label=day_label,
            model=gemini_override,
        )
        result["combined_analysis_ok"] = bool(combined.get("ok"))
        _note_gemini_failure(result, combined, "combined")
        if not combined.get("ok"):
            result["combined_analysis_error"] = combined.get("error")
            logger.error("Combined analysis failed: %s", combined.get("error"))
        else:
            combined_model = combined.get("model") or result["models"]["combined"]
            has_food_att = bool(food_archive.get("ok") and food_archive.get("content"))
            html_body = _daily_email_html(
                day_label=day_label,
                analysis_result=combined,
                summary=summary,
                model=combined_model,
                has_food_attachment=has_food_att,
            )
            outcome_up = storage_client.upload_to_outcomes(f"{file_stem}.html", html_body)
            result["storage_outcomes"] = outcome_up
            if outcome_up.get("ok"):
                logger.info("GCS outcomes saved: %s", outcome_up.get("gs_uri"))
            else:
                logger.error("GCS outcomes failed: %s", outcome_up)
    elif save_reports:
        result["storage_skipped"] = "gcs_not_configured"
        logger.warning("GCS save skipped: GCS_REPORTS_BUCKET not set")

    if send:
        if not combined.get("ok"):
            result["emailed"] = False
            result["email_error"] = combined.get("error") or "combined_analysis_not_available"
        elif not email_client.is_configured():
            result["emailed"] = False
            result["email_error"] = "smtp_not_configured"
        else:
            subject = f"MyBody — дневной отчёт за {day_label}"
            text_alt = combined.get("analysis") or "Отчёт MyBody (откройте в HTML-клиенте)."
            email_attachments: list[dict[str, str]] = []
            if food_archive.get("ok") and food_archive.get("content"):
                food_object = food_archive.get("object") or ""
                food_filename = food_object.rsplit("/", 1)[-1] if food_object else f"{file_stem}-food.md"
                email_attachments.append(
                    {
                        "filename": food_filename,
                        "content": food_archive["content"],
                        "content_type": "text/markdown; charset=utf-8",
                    }
                )
            sent = email_client.send_email(
                subject,
                html_body,
                text_body=text_alt,
                attachments=email_attachments or None,
            )
            result["emailed"] = bool(sent.get("ok"))
            if sent.get("ok"):
                result["recipients"] = sent.get("to")
                if sent.get("attachments"):
                    result["email_attachments"] = sent["attachments"]
            else:
                result["email_error"] = sent.get("error")
                result["email_detail"] = sent.get("detail")

    status = _daily_report_http_status(result, send=send, save_reports=save_reports)
    if result.get("gemini_daily_quota"):
        result["ok"] = False
        result.setdefault("partial", True)
    return JSONResponse(result, status_code=status)


@app.post("/internal/storage-probe")
def storage_probe(x_cron_secret: str | None = Header(None, alias="X-Cron-Secret")):
    """Диагностика GCS: пробная запись в bucket отчётов."""
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    result = storage_client.probe_access()
    status = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status)


@app.post("/internal/meals-probe")
def meals_probe(
    day: str | None = None,
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
):
    """Диагностика чтения папки Meals на Google Drive (опционально — подпапка за day=YYYY-MM-DD)."""
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    result = drive_client.probe_meals_access(day=day)
    status = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status)


@app.post("/internal/drive-probe")
def drive_probe(x_cron_secret: str | None = Header(None, alias="X-Cron-Secret")):
    """Диагностика Google Drive: SA email и пробная запись в папки Shorts/Detailed."""
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    result = drive_client.probe_access()
    status = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status)


@app.post("/internal/garmin-fetch")
def garmin_fetch(
    days: int = 1,
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
):
    """
    Загружает данные из Garmin Connect за последние `days` дней.
    Вызов защищён: нужен заголовок X-Cron-Secret (значение из env CRON_SECRET).
    Для вызова по расписанию (Cloud Scheduler) задайте CRON_SECRET в Cloud Run.
    """
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")
    data = garmin_client.fetch_recent_data(days=days)
    return data
