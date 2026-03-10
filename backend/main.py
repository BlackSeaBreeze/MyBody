"""
MyBody — бэкенд: ежедневные рекомендации и чат-агент на Gemini.
Данные из приложения (фото еды, витамины, активность, Garmin) — один контекст для отчёта и чата.
"""
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend import garmin_client

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


# --- Garmin ---

@app.get("/garmin/status")
def garmin_status():
    """Проверка: заданы ли учётные данные Garmin (без попытки логина)."""
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    configured = bool(email and os.environ.get("GARMIN_PASSWORD", "").strip())
    return {"garmin_configured": configured}


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
