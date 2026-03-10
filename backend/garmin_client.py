"""
Клиент для загрузки данных из Garmin Connect (активность, сон, статистика).
Учётные данные берутся из переменных окружения GARMIN_EMAIL и GARMIN_PASSWORD.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

# Ленивый импорт: garminconnect ставится опционально для окружений без Garmin
try:
    from garminconnect import Garmin
except ImportError:
    Garmin = None  # type: ignore


def get_client() -> Any:
    """Создаёт и логинит клиент Garmin. Возвращает None, если креды не заданы или ошибка."""
    if Garmin is None:
        return None
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()
    if not email or not password:
        return None
    try:
        api = Garmin(email, password)
        api.login()
        return api
    except Exception:
        return None


def fetch_recent_data(days: int = 1) -> dict[str, Any]:
    """
    Загружает данные за последние days дней: активность и статистика.
    Возвращает словарь, готовый для передачи в агента/отчёт.
    """
    api = get_client()
    if api is None:
        return {"ok": False, "error": "garmin_not_configured", "data": None}

    try:
        end = datetime.now().date()
        start = end - timedelta(days=days)

        # Активности за период (get_activities_by_date(startdate, enddate))
        activity_list = []
        try:
            activities = api.get_activities_by_date(
                startdate=start.isoformat(),
                enddate=end.isoformat(),
            )
            if isinstance(activities, list):
                for a in activities:
                    activity_list.append({
                        "activityName": a.get("activityName"),
                        "activityType": a.get("activityType", {}).get("typeKey") if isinstance(a.get("activityType"), dict) else None,
                        "startTime": a.get("startTimeGMT") or a.get("beginTimestamp"),
                        "duration": a.get("duration"),
                        "distance": a.get("distance"),
                        "calories": a.get("calories"),
                        "averageHR": a.get("averageHR"),
                    })
        except Exception:
            pass

        # Статистика по дням (шаги, сон и т.д. — get_stats(date))
        stats_by_day = {}
        for d in range((end - start).days + 1):
            day = start + timedelta(days=d)
            day_str = day.isoformat()
            try:
                stats = api.get_stats(day_str)
                if isinstance(stats, dict):
                    stats_by_day[day_str] = stats
            except Exception:
                pass

        return {
            "ok": True,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "activities": activity_list,
            "stats_by_day": stats_by_day,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "data": None}
