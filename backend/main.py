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
    if summary.get("mode") == "morning_report":
        return _morning_report_table_html(summary)

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


def _morning_report_table_html(summary: dict) -> str:
    """Утренний отчёт: активность за вчера + сон одной ночи (без путаницы двух дат)."""
    th = 'style="border:1px solid #ddd;padding:6px 10px;text-align:left;background:#f3f4f6;font-size:13px;"'
    td = 'style="border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:13px;"'
    activity = summary.get("activity") or {}
    sleep = summary.get("sleep") or {}
    activity_day = summary.get("activity_day") or activity.get("date") or "—"
    sleep_day = summary.get("sleep_day") or sleep.get("sleep_day") or "—"

    act_row = (
        "<tr>"
        f'<td {td}><strong>Активность ({html.escape(str(activity_day))})</strong></td>'
        f'<td {td}>{html.escape(str(activity.get("steps") or "—"))}</td>'
        f'<td {td}>—</td>'
        f'<td {td}>{activity.get("calories_total") if activity.get("calories_total") is not None else "—"}</td>'
        f'<td {td}>{(str(activity.get("distance_km")) + " км") if activity.get("distance_km") is not None else "—"}</td>'
        f'<td {td}>{html.escape(str(activity.get("stress") or "—"))}</td>'
        f'<td {td}>{html.escape(str(activity.get("body_battery") or "—"))}</td>'
        f'<td {td}>{activity.get("resting_hr") if activity.get("resting_hr") is not None else "—"}</td>'
        "</tr>"
    )
    activity_table = (
        '<table style="border-collapse:collapse;width:100%;margin:8px 0 12px;">'
        "<thead><tr>"
        f"<th {th}>Период</th><th {th}>Шаги / цель</th><th {th}>Сон</th><th {th}>Ккал</th>"
        f"<th {th}>Расстояние</th><th {th}>Стресс</th><th {th}>Body Battery</th><th {th}>Пульс покоя</th>"
        f"</tr></thead><tbody>{act_row}</tbody></table>"
    )

    vo2 = summary.get("vo2_max") or {}
    vo2_note = ""
    if vo2.get("found") and vo2.get("display"):
        vo2_note = (
            f'<p style="font-size:13px;color:#374151;margin:0 0 12px;">'
            f"<strong>VO2 max</strong> ({html.escape(str(activity_day))}): "
            f"{html.escape(str(vo2.get('display')))}</p>"
        )

    if not sleep.get("found"):
        return activity_table + vo2_note + '<p style="color:#888;font-size:13px;">Сон: нет данных за эту ночь.</p>'

    def _cell(label: str, val: Any) -> str:
        display = "—" if val is None or val == "" else html.escape(str(val))
        return f"<tr><td {td}><strong>{html.escape(label)}</strong></td><td {td}>{display}</td></tr>"

    sleep_rows = "".join(
        [
            _cell("Ночь (пробуждение)", sleep_day),
            _cell(
                "Всего сна",
                sleep.get("total_sleep_hm")
                or garmin_client.format_sleep_duration(minutes=sleep.get("total_sleep_min")),
            ),
            _cell(
                "Глубокий сон",
                garmin_client.format_sleep_duration(minutes=sleep.get("deep_sleep_min"))
                if sleep.get("deep_sleep_min") is not None
                else None,
            ),
            _cell(
                "Лёгкий сон",
                garmin_client.format_sleep_duration(minutes=sleep.get("light_sleep_min"))
                if sleep.get("light_sleep_min") is not None
                else None,
            ),
            _cell(
                "REM",
                garmin_client.format_sleep_duration(minutes=sleep.get("rem_sleep_min"))
                if sleep.get("rem_sleep_min") is not None
                else None,
            ),
            _cell(
                "Бодрствование",
                garmin_client.format_sleep_duration(minutes=sleep.get("awake_min"))
                if sleep.get("awake_min") is not None
                else None,
            ),
            _cell("Пробуждения", sleep.get("restless_moments")),
            _cell("Оценка сна", sleep.get("sleep_score_overall")),
            _cell(
                "Качество",
                sleep.get("sleep_quality_label")
                or sleep.get("sleep_quality_type")
                or sleep.get("sleep_score_quality")
                or sleep.get("sleep_score_stress"),
            ),
            _cell("Отбой → подъём", f'{sleep.get("bedtime") or "?"} → {sleep.get("wake_time") or "?"}'),
            _cell("Пульс во сне (ср.)", sleep.get("avg_hr_sleep")),
            _cell("Стресс во сне (ср.)", sleep.get("avg_sleep_stress")),
        ]
    )
    sleep_table = (
        f'<p style="font-size:14px;font-weight:bold;margin:12px 0 6px;color:#374151;">'
        f"Сон (ночь → {html.escape(str(sleep_day))})</p>"
        f'<table style="border-collapse:collapse;width:100%;margin:0 0 16px;">'
        f"<tbody>{sleep_rows}</tbody></table>"
    )
    return activity_table + vo2_note + sleep_table


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

    <h2 style="font-size:16px;margin:22px 0 10px;color:#374151;">Коучинг на сегодня</h2>
    {analysis_html}
    {attachment_note}

    <div style="color:#aaa;font-size:12px;margin-top:20px;">Сформировано автоматически сервисом MyBody.</div>
  </div>
</body>
</html>"""


def _cloud_run_logs_url() -> str | None:
    """Ссылка на логи сервиса в GCP Console (Cloud Run задаёт K_SERVICE автоматически)."""
    custom = os.environ.get("CLOUD_RUN_LOGS_URL", "").strip()
    if custom:
        return custom
    service = os.environ.get("K_SERVICE", "").strip()
    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        or os.environ.get("GCP_PROJECT", "").strip()
    )
    region = os.environ.get("CLOUD_RUN_REGION", "europe-west1").strip() or "europe-west1"
    if service and project:
        return (
            f"https://console.cloud.google.com/run/detail/{region}/{service}/logs"
            f"?project={project}"
        )
    return None


def _failure_alerts_enabled() -> bool:
    return os.environ.get("PIPELINE_FAILURE_ALERTS", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _maybe_send_failure_alert(
    result: dict[str, Any],
    *,
    report_type: str,
    period_label: str,
    send: bool,
) -> None:
    """Письмо-алерт, если отчёт не ушёл получателю (send=true, emailed≠true)."""
    if not send or not _failure_alerts_enabled():
        return
    if result.get("emailed"):
        return
    if not email_client.is_configured():
        result["failure_alert_skipped"] = "smtp_not_configured"
        return

    logs_url = _cloud_run_logs_url()
    title_adj = "дневного" if report_type == "daily" else "недельного"
    subject = f"MyBody — сбой {title_adj} отчёта ({period_label})"
    if result.get("gemini_daily_quota"):
        subject += " [квота Gemini]"

    html_body = report_formats.build_pipeline_failure_email_html(
        report_type=report_type,
        period_label=period_label,
        result=result,
        logs_url=logs_url,
    )
    text_body = report_formats.build_pipeline_failure_text(
        report_type=report_type,
        period_label=period_label,
        result=result,
        logs_url=logs_url,
    )
    sent = email_client.send_email(subject, html_body, text_body=text_body)
    result["failure_alert_emailed"] = bool(sent.get("ok"))
    if sent.get("ok"):
        result["failure_alert_recipients"] = sent.get("to")
        logger.info("Pipeline failure alert sent (%s, %s)", report_type, period_label)
    else:
        result["failure_alert_error"] = sent.get("error")
        result["failure_alert_detail"] = sent.get("detail")
        logger.error("Pipeline failure alert failed: %s", sent.get("error"))

    if not result.get("emailed") and not result.get("failure_alert_emailed"):
        reason = (
            result.get("failure_alert_skipped")
            or result.get("failure_alert_error")
            or ("alerts_disabled" if not _failure_alerts_enabled() else "unknown")
        )
        logger.error(
            "MYBODY_SAFETY_NET report_type=%s period=%s reason=%s",
            report_type,
            period_label,
            reason,
        )


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
    # Cron 2xx: пользователь уведомлён письмом о сбое (п.1) — не дублировать GCP Scheduler alert
    if result.get("failure_alert_emailed"):
        return 200
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


def _load_medical_data(result: dict) -> str | None:
    """Читает медицинские данные из Drive Notes/medical_data; возвращает текст или None."""
    if not drive_client.is_notes_configured():
        result["medical_data_skipped"] = "drive_notes_not_configured"
        return None

    notes = drive_client.fetch_medical_data()
    result["medical_data_fetch"] = {
        "ok": notes.get("ok"),
        "filename": notes.get("filename"),
        "file_found": notes.get("file_found"),
        "char_count": notes.get("char_count", 0),
        "mime_type": notes.get("mime_type"),
        "error": notes.get("error"),
    }
    if not notes.get("ok"):
        logger.error("Medical data fetch failed: %s", notes.get("error"))
        return None
    if not notes.get("file_found"):
        result["medical_data_skipped"] = "file_not_found"
        return None
    text = notes.get("text")
    if not text or not str(text).strip():
        result["medical_data_skipped"] = "empty_file"
        return None
    return str(text).strip()


def _load_general_notes(result: dict) -> str | None:
    """Читает постоянные пожелания из Drive Notes/general; возвращает текст или None."""
    if not drive_client.is_notes_configured():
        result["general_notes_skipped"] = "drive_notes_not_configured"
        return None

    notes = drive_client.fetch_general_notes()
    result["general_notes_fetch"] = {
        "ok": notes.get("ok"),
        "filename": notes.get("filename"),
        "file_found": notes.get("file_found"),
        "char_count": notes.get("char_count", 0),
        "mime_type": notes.get("mime_type"),
        "error": notes.get("error"),
    }
    if not notes.get("ok"):
        logger.error("General notes fetch failed: %s", notes.get("error"))
        return None
    if not notes.get("file_found"):
        result["general_notes_skipped"] = "file_not_found"
        return None
    text = notes.get("text")
    if not text or not str(text).strip():
        result["general_notes_skipped"] = "empty_file"
        return None
    return str(text).strip()


def _load_daily_notes(day_label: str, result: dict) -> str | None:
    """Читает заметки за день из Drive Notes/YYYY.MM.DD; возвращает текст или None."""
    if not drive_client.is_notes_configured():
        result["notes_skipped"] = "drive_notes_not_configured"
        return None

    notes = drive_client.fetch_day_notes(day_label)
    result["notes_fetch"] = {
        "ok": notes.get("ok"),
        "filename": notes.get("filename"),
        "file_found": notes.get("file_found"),
        "char_count": notes.get("char_count", 0),
        "mime_type": notes.get("mime_type"),
        "error": notes.get("error"),
    }
    if not notes.get("ok"):
        logger.error("Notes fetch failed: %s", notes.get("error"))
        return None
    if not notes.get("file_found"):
        result["notes_skipped"] = "file_not_found"
        return None
    text = notes.get("text")
    if not text or not str(text).strip():
        result["notes_skipped"] = "empty_file"
        return None
    return str(text).strip()


def _save_food_analysis(
    *,
    day_label: str,
    file_stem: str,
    model: str | None,
    metrics: dict,
    result: dict,
    report_day: str | None = None,
    general_notes: str | None = None,
    medical_notes: str | None = None,
) -> None:
    """Загружает фото еды из Drive за food_day (вчера), анализирует Gemini, сохраняет в GCS archive."""
    if not drive_client.is_meals_configured():
        result["meals_skipped"] = "drive_meals_not_configured"
        return

    meals = drive_client.fetch_day_meal_photos(day_label)
    result["meals_fetch"] = {
        "ok": meals.get("ok"),
        "folder_name": meals.get("folder_name"),
        "folder_found": meals.get("folder_found"),
        "images_found": meals.get("images_found", 0),
        "photo_count": meals.get("photo_count", 0),
        "photos_skipped_count": meals.get("photos_skipped_count", 0),
        "analysis_complete": meals.get("analysis_complete", True),
        "files_in_folder_count": len(meals.get("files_in_folder") or []),
        "non_image_files": meals.get("non_image_files"),
        "skipped": meals.get("skipped"),
        "error": meals.get("error"),
    }
    if meals.get("images_found", 0) > meals.get("photo_count", 0):
        logger.warning(
            "Meals: incomplete fetch for %s — loaded %s of %s images",
            day_label,
            meals.get("photo_count"),
            meals.get("images_found"),
        )
    if meals.get("photos_skipped_count"):
        logger.warning(
            "Meals: %s/%s photos skipped for %s: %s",
            meals.get("photos_skipped_count"),
            meals.get("images_found"),
            day_label,
            meals.get("skipped"),
        )
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
        report_day=report_day,
        model=model,
        profile_hint=_profile_hint_from_metrics(metrics),
        general_notes=general_notes,
        medical_notes=medical_notes,
        fetch_meta=meals,
    )
    result["food_analysis_ok"] = bool(food.get("ok"))
    _note_gemini_failure(result, food, "food")
    if not food.get("ok"):
        result["food_analysis_error"] = food.get("error")
        logger.error("Food Gemini analysis failed: %s", food.get("error"))
        return

    food_analysis_text = food["analysis"]
    arith = report_formats.validate_food_arithmetic(food_analysis_text)
    result["food_arithmetic_check"] = arith
    arith_warn = report_formats.food_arithmetic_warning(food_analysis_text)
    if arith_warn:
        logger.warning("Food arithmetic check failed: %s", arith.get("issues"))
        food_analysis_text = arith_warn + "\n\n" + food_analysis_text

    food_md = report_formats.build_food_report_md(
        day_label=day_label,
        food_analysis=food_analysis_text,
        model=food.get("model") or gemini_client.model_for_step("food", model),
        photo_meta=meals,
        context_meta=food.get("context"),
    )
    food_name = f"{file_stem}-food.md"
    food_up = storage_client.upload_to_archive(food_name, food_md)
    result["storage_archive_food"] = food_up
    result["food_archive_content"] = food_md
    result["food_archive_object"] = food_up.get("object") if food_up.get("ok") else food_name
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
    Утренний отчёт после пробуждения (report_day = D).
    По умолчанию D — сегодня в REPORT_TIMEZONE. Явно: ?day=YYYY-MM-DD (дата пробуждения).

    Источники: Garmin активность и заметки за D−1; фото еды за D−1; сон — прошедшая ночь (sleep_data на D).

    Порядок:
    1) Garmin + заметки за вчера → Gemini → archive/vb-….md
    2) Фото еды за вчера → archive/vb-…-food.md
    3) Garmin + food-архив + заметки → итоговый Gemini → outcomes/vb-….html + email

    Вызов защищён: при заданном CRON_SECRET требуется заголовок X-Cron-Secret.
    """
    if save_drive is not None:
        save_reports = save_drive

    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    metrics = garmin_client.fetch_daily_metrics(day=day)
    day_label = str(metrics.get("report_day") or metrics.get("to") or day or "сегодня")
    food_day = str(metrics.get("food_day") or day_label)
    gemini_override = model.strip() if model else None
    file_stem = report_formats.drive_file_stem(day_label=day_label)
    result: dict = {
        "ok": bool(metrics.get("ok")),
        "day": day_label,
        "report_day": day_label,
        "food_day": food_day,
        "activity_day": str(metrics.get("activity_day") or food_day),
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
        _maybe_send_failure_alert(
            result, report_type="daily", period_label=day_label, send=send
        )
        return JSONResponse(result, status_code=502)

    summary = garmin_client.summary_from_metrics(metrics)
    html_body = ""
    combined: dict = {"ok": False, "error": "not_run"}
    food_archive: dict[str, Any] = {}
    daily_notes = _load_daily_notes(food_day, result)
    general_notes = _load_general_notes(result)
    medical_notes = _load_medical_data(result)

    if save_reports and storage_client.is_configured():
        # 1) Garmin + заметки — архивный экспертный анализ (один вызов Gemini)
        detailed = gemini_client.analyze_garmin_metrics_detailed(
            metrics,
            model=gemini_override,
            daily_notes=daily_notes,
            general_notes=general_notes,
            medical_notes=medical_notes,
        )
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
            day_label=food_day,
            file_stem=file_stem,
            report_day=day_label,
            model=gemini_override,
            metrics=metrics,
            result=result,
            general_notes=general_notes,
            medical_notes=medical_notes,
        )

        result["gemini_inter_call_delay_sec"] = _gemini_sleep()

        # 3) Food-архив (только что сохранённый) + сырые Garmin → итоговый отчёт
        food_archive: dict[str, Any] = {}
        food_analysis_text: str | None = None
        food_archive_content = result.get("food_archive_content")
        if food_archive_content:
            food_analysis_text = report_formats.extract_analysis_section(food_archive_content)
            food_archive = {
                "ok": True,
                "content": food_archive_content,
                "object": result.get("food_archive_object"),
                "source": "current_run",
            }
            result["food_archive_used"] = {
                "object": result.get("food_archive_object"),
                "source": "current_run",
            }
        elif result.get("food_analysis_error"):
            result["food_archive_used"] = {
                "ok": False,
                "error": "food_analysis_failed",
                "detail": "Stale GCS archive skipped — food step failed this run",
                "source": "none",
            }
            logger.warning(
                "Skipping stale food archive for %s: food analysis failed (%s)",
                day_label,
                str(result.get("food_analysis_error", ""))[:120],
            )
        else:
            food_archive = storage_client.get_latest_food_analysis_for_day(day_label)
            if food_archive.get("ok"):
                food_analysis_text = report_formats.extract_analysis_section(food_archive["content"])
                fm = report_formats.extract_food_frontmatter(food_archive["content"])
                stale_model = str(fm.get("model") or "")
                stale_generated = fm.get("generated_at_utc")
                stale_photos = fm.get("photos_analyzed")
                current_found = (result.get("meals_fetch") or {}).get("images_found")
                stale_incomplete = (
                    "lite" in stale_model.lower()
                    or fm.get("analysis_complete") is False
                    or (
                        current_found
                        and stale_photos
                        and int(stale_photos) < int(current_found)
                    )
                )
                result["food_archive_used"] = {
                    "object": food_archive.get("object"),
                    "gs_uri": food_archive.get("gs_uri"),
                    "candidates_count": food_archive.get("candidates_count"),
                    "source": "gcs_latest",
                    "stale_model": stale_model or None,
                    "stale_generated_at_utc": stale_generated,
                    "stale_photos_analyzed": stale_photos,
                    "stale_incomplete": stale_incomplete,
                }
                if stale_incomplete:
                    food_analysis_text = (
                        f"⚠ Используется устаревший архив питания ({stale_generated or 'unknown'}, "
                        f"модель {stale_model or '?'}, фото {stale_photos or '?'}). "
                        f"Перезапустите food-анализ после сброса квоты Gemini.\n\n"
                        + food_analysis_text
                    )
                    logger.warning(
                        "Stale incomplete food archive for %s: model=%s photos=%s/%s",
                        day_label,
                        stale_model,
                        stale_photos,
                        current_found,
                    )
            else:
                result["food_archive_used"] = {
                    "ok": False,
                    "error": food_archive.get("error"),
                }
                logger.warning("No food archive for combined report: %s", food_archive.get("error"))

        if food_analysis_text:
            mf = result.get("meals_fetch") or {}
            if mf.get("photos_skipped_count") or mf.get("analysis_complete") is False:
                skipped_n = mf.get("photos_skipped_count") or 0
                found_n = mf.get("images_found") or "?"
                food_analysis_text = (
                    f"⚠ Анализ питания неполный: проанализировано {mf.get('photo_count')} из {found_n} фото "
                    f"({skipped_n} пропущено при загрузке). Выводы по калориям/дефицитам могут быть занижены.\n\n"
                    + food_analysis_text
                )

        combined = gemini_client.analyze_daily_combined(
            metrics,
            food_analysis=food_analysis_text,
            daily_notes=daily_notes,
            day_label=day_label,
            food_day=food_day,
            general_notes=general_notes,
            medical_notes=medical_notes,
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
    _maybe_send_failure_alert(
        result, report_type="daily", period_label=day_label, send=send
    )
    return JSONResponse(result, status_code=status)


@app.post("/internal/weekly-report")
def weekly_report(
    end_day: str | None = None,
    days: int = 7,
    model: str | None = None,
    send: bool = True,
    save_reports: bool = True,
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
):
    """
    Недельный мета-анализ по сохранённым GCS-архивам (Garmin + food за days дней).

    По умолчанию end_day — сегодня в REPORT_TIMEZONE; days=7.
    Контекст: полные MD; при превышении WEEKLY_MAX_INPUT_TOKENS (220k) — FACT-digest.
    В письме — disclaimer, если контекст был сокращён.

    Рекомендуемый cron: на 1 ч позже daily-report (например вс 10:00 Europe/Dublin).
    """
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    end_day_label = end_day.strip() if end_day else str(garmin_client._default_report_day())
    days_count = min(max(1, days), 31)
    gemini_override = model.strip() if model else None
    file_stem = report_formats.weekly_file_stem(end_day_label=end_day_label)
    period_label = f"{end_day_label} (−{days_count} дн.)"

    result: dict[str, Any] = {
        "ok": False,
        "report_type": "weekly",
        "end_day": end_day_label,
        "days": days_count,
        "file_stem": file_stem,
        "model": gemini_override or gemini_client.model_for_step("weekly", None),
    }

    if not storage_client.is_configured():
        result["error"] = "gcs_not_configured"
        _maybe_send_failure_alert(
            result, report_type="weekly", period_label=period_label, send=send
        )
        return JSONResponse(result, status_code=502)

    archives = storage_client.fetch_weekly_archives(end_day=end_day_label, days=days_count)
    result["archives"] = {
        "start_day": archives.get("start_day"),
        "end_day": archives.get("end_day"),
        "garmin_days_found": archives.get("garmin_days_found"),
        "food_days_found": archives.get("food_days_found"),
        "missing": archives.get("missing"),
    }

    if not archives.get("ok"):
        result["error"] = archives.get("error")
        result["detail"] = archives.get("day_labels")
        _maybe_send_failure_alert(
            result, report_type="weekly", period_label=period_label, send=send
        )
        return JSONResponse(result, status_code=502)

    start_day_label = str(archives.get("start_day") or end_day_label)
    period_label = f"{start_day_label} — {end_day_label}"

    context_prep = report_formats.prepare_weekly_gemini_context(
        garmin_archives=archives["garmin_archives"],
        food_archives=archives.get("food_archives") or [],
    )
    result["context_mode"] = context_prep.get("context_mode")
    result["input_est_tokens"] = context_prep.get("est_tokens")
    result["input_est_tokens_full"] = context_prep.get("est_tokens_full")
    result["free_tier_fallback"] = context_prep.get("free_tier_fallback")

    general_notes = _load_general_notes(result)
    medical_notes = _load_medical_data(result)

    weekly = gemini_client.analyze_weekly_archives(
        context_text=context_prep["text"],
        context_prep=context_prep,
        start_day=start_day_label,
        end_day=end_day_label,
        general_notes=general_notes,
        medical_notes=medical_notes,
        model=gemini_override,
    )
    result["weekly_analysis_ok"] = bool(weekly.get("ok"))
    _note_gemini_failure(result, weekly, "weekly")

    html_body = ""
    if weekly.get("ok"):
        result["ok"] = True
        weekly_model = weekly.get("model") or result["model"]
        result["model"] = weekly_model
        html_body = report_formats.build_weekly_email_html(
            period_label=period_label,
            analysis_result=weekly,
            model=weekly_model,
            context_prep=context_prep,
            archives_meta=archives,
        )
        if save_reports:
            outcome_up = storage_client.upload_to_outcomes(f"{file_stem}.html", html_body)
            result["storage_outcomes"] = outcome_up
            if outcome_up.get("ok"):
                logger.info("GCS weekly outcome saved: %s", outcome_up.get("gs_uri"))
            else:
                logger.error("GCS weekly outcome failed: %s", outcome_up)
    else:
        result["error"] = weekly.get("error")
        result["weekly_analysis_error"] = weekly.get("error")

    if send:
        if not weekly.get("ok"):
            result["emailed"] = False
            result["email_error"] = weekly.get("error") or "weekly_analysis_not_available"
        elif not email_client.is_configured():
            result["emailed"] = False
            result["email_error"] = "smtp_not_configured"
        else:
            subject = f"MyBody — недельный отчёт ({start_day_label} — {end_day_label})"
            if context_prep.get("free_tier_fallback"):
                subject += " [сокращённый контекст]"
            text_alt = weekly.get("analysis") or "Недельный отчёт MyBody."
            if context_prep.get("free_tier_fallback"):
                text_alt = (
                    "⚠ Контекст для Gemini был сокращён до FACT-digest (полные архивы в GCS).\n\n"
                    + text_alt
                )
            sent = email_client.send_email(subject, html_body, text_body=text_alt)
            result["emailed"] = bool(sent.get("ok"))
            if sent.get("ok"):
                result["recipients"] = sent.get("to")
            else:
                result["email_error"] = sent.get("error")
                result["email_detail"] = sent.get("detail")

    if result.get("gemini_daily_quota"):
        result["ok"] = False
        result.setdefault("partial", True)

    status = 200 if result.get("ok") else (503 if gemini_client.is_retryable_gemini_error(str(result.get("error"))) else 502)
    if result.get("failure_alert_emailed"):
        status = 200
    elif result.get("ok") and send and result.get("email_error") and not result.get("emailed"):
        status = 502
    _maybe_send_failure_alert(
        result, report_type="weekly", period_label=period_label, send=send
    )
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


@app.post("/internal/notes-probe")
def notes_probe(
    day: str | None = None,
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
):
    """Диагностика чтения папки Notes (general, medical_data + опционально day=YYYY-MM-DD)."""
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    result = drive_client.probe_notes_access(day=day)
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
