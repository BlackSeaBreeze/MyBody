"""
MyBody — бэкенд: ежедневные рекомендации и чат-агент на Gemini.
Данные из приложения (фото еды, витамины, активность, Garmin) — один контекст для отчёта и чата.
"""
import html
import os
import re
import time

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from backend import drive_client, email_client, garmin_client, gemini_client
from backend import report_formats
from backend.gemini_client import DEFAULT_GEMINI_MODEL


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


def _daily_email_html(*, day_label: str, analysis_result: dict, summary: dict, model: str) -> str:
    """Светлое email-оформление: заголовок, таблица показателей, AI-анализ."""
    ctx = analysis_result.get("context") or {}
    meta_bits = [f"период: {html.escape(day_label)}", f"модель: {html.escape(model)}"]
    if ctx.get("est_tokens") is not None:
        meta_bits.append(f"~{ctx.get('est_tokens'):,} токенов".replace(",", " "))
    meta = " · ".join(meta_bits)

    if analysis_result.get("ok") and analysis_result.get("analysis"):
        analysis_html = _markdown_to_html(analysis_result["analysis"])
    else:
        err = html.escape(str(analysis_result.get("error", "unknown")))
        analysis_html = f'<p style="color:#a33;">Анализ недоступен: {err}</p>'

    table_html = _summary_table_html(summary)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;background:#f5f6f8;">
  <div style="max-width:680px;margin:0 auto;padding:24px 20px;font-family:Arial,Helvetica,sans-serif;color:#222;line-height:1.55;">
    <h1 style="font-size:20px;margin:0 0 4px;">MyBody — отчёт Garmin</h1>
    <div style="color:#888;font-size:13px;margin-bottom:20px;">{meta}</div>

    <h2 style="font-size:16px;margin:18px 0 6px;">Показатели за день</h2>
    {table_html}

    <h2 style="font-size:16px;margin:18px 0 6px;">Анализ и рекомендации</h2>
    <div style="background:#fff;border:1px solid #e3e5e8;border-radius:10px;padding:16px 18px;">
      {analysis_html}
    </div>

    <div style="color:#aaa;font-size:12px;margin-top:20px;">Сформировано автоматически сервисом MyBody.</div>
  </div>
</body>
</html>"""


@app.post("/internal/daily-report")
def daily_report(
    day: str | None = None,
    model: str | None = None,
    send: bool = True,
    save_drive: bool = True,
    x_cron_secret: str | None = Header(None, alias="X-Cron-Secret"),
):
    """
    Ежедневный отчёт: метрики Garmin за один день (по умолчанию сегодня; сон = прошедшая ночь),
    краткий анализ Gemini, подробный архивный анализ, письмо на MAIL_TO и сохранение на Google Drive.

    Drive Shorts: HTML (как в письме). Drive Detailed: Markdown с подробным анализом + JSON метрик.

    Вызов защищён: при заданном CRON_SECRET требуется заголовок X-Cron-Secret.
    Параметры: day, model, send=false, save_drive=false.
    """
    secret = os.environ.get("CRON_SECRET", "").strip()
    if secret and x_cron_secret != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Cron-Secret")

    metrics = garmin_client.fetch_daily_metrics(day=day)
    day_label = str(metrics.get("to") or day or "сегодня")
    model = (model or DEFAULT_GEMINI_MODEL).strip()
    file_stem = report_formats.drive_file_stem()
    result: dict = {"ok": bool(metrics.get("ok")), "day": day_label, "file_stem": file_stem, "model": model}

    if not metrics.get("ok"):
        result["error"] = metrics.get("error")
        if metrics.get("detail"):
            result["detail"] = metrics["detail"]
        if metrics.get("hint"):
            result["hint"] = metrics["hint"]
        return JSONResponse(result, status_code=502)

    analysis = gemini_client.analyze_garmin_metrics(metrics, model=model)
    summary = garmin_client.summary_from_metrics(metrics)
    result["analysis_ok"] = bool(analysis.get("ok"))
    if not analysis.get("ok"):
        result["analysis_error"] = analysis.get("error")

    html_body = _daily_email_html(
        day_label=day_label, analysis_result=analysis, summary=summary, model=model
    )

    if send:
        if not email_client.is_configured():
            result["emailed"] = False
            result["email_error"] = "smtp_not_configured"
        else:
            subject = f"MyBody — отчёт Garmin за {day_label}"
            text_alt = analysis.get("analysis") or "Отчёт MyBody (откройте в HTML-клиенте)."
            sent = email_client.send_email(subject, html_body, text_body=text_alt)
            result["emailed"] = bool(sent.get("ok"))
            if sent.get("ok"):
                result["recipients"] = sent.get("to")
            else:
                result["email_error"] = sent.get("error")
                result["email_detail"] = sent.get("detail")

    if save_drive and drive_client.is_configured():
        short_name = f"{file_stem}.html"
        short_up = drive_client.upload_to_shorts(short_name, html_body)
        result["drive_shorts"] = short_up

        delay = _gemini_inter_call_delay_sec()
        if delay > 0:
            time.sleep(delay)
        result["gemini_inter_call_delay_sec"] = delay

        detailed = gemini_client.analyze_garmin_metrics_detailed(metrics, model=model)
        result["detailed_analysis_ok"] = bool(detailed.get("ok"))
        if detailed.get("ok"):
            detailed_md = report_formats.build_detailed_report_md(
                day_label=day_label,
                detailed_analysis=detailed["analysis"],
                metrics=metrics,
                summary=summary,
                model=model,
                context_meta=detailed.get("context"),
            )
            detailed_name = f"{file_stem}.md"
            detailed_up = drive_client.upload_to_detailed(detailed_name, detailed_md)
            result["drive_detailed"] = detailed_up
        else:
            result["detailed_analysis_error"] = detailed.get("error")
            result["drive_detailed"] = {"ok": False, "error": "detailed_analysis_failed"}
    elif save_drive:
        result["drive_skipped"] = "drive_not_configured"

    if not result.get("ok"):
        return JSONResponse(result, status_code=502)
    if send and result.get("email_error") and not result.get("emailed"):
        return JSONResponse(result, status_code=502)
    if save_drive and result.get("drive_shorts", {}).get("ok") is False:
        return JSONResponse(result, status_code=502)

    return JSONResponse(result)


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
