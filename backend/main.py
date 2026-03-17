"""
MyBody — бэкенд: ежедневные рекомендации и чат-агент на Gemini.
Данные из приложения (фото еды, витамины, активность, Garmin) — один контекст для отчёта и чата.
"""
import os

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from backend import garmin_client, gemini_client

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
def garmin_analyze(days: int = 7, model: str = "gemini-3-flash-preview"):
    """
    Загружает полные метрики Garmin за последние days дней, отправляет их в Gemini
    и возвращает текстовый анализ и рекомендации по здоровью и активности.
    Требуется переменная окружения GEMINI_API_KEY (ключ из Google AI Studio).
    """
    metrics = garmin_client.fetch_all_metrics(days=min(max(1, days), 31))
    result = gemini_client.analyze_garmin_metrics(metrics, model=model)
    return result


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
