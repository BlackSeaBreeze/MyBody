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

        # Длительность сна как на странице Sleep (get_sleep_data → sleepTimeSeconds)
        sleep_duration_by_day: dict[str, int] = {}
        for day_str in list(stats_by_day.keys()):
            try:
                sleep_data = api.get_sleep_data(day_str)
                if isinstance(sleep_data, dict):
                    dto = sleep_data.get("dailySleepDTO") or sleep_data
                    sec = dto.get("sleepTimeSeconds") if isinstance(dto, dict) else None
                    if sec is not None:
                        sleep_duration_by_day[day_str] = int(sec)
            except Exception:
                pass

        return {
            "ok": True,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "activities": activity_list,
            "stats_by_day": stats_by_day,
            "sleep_duration_by_day": sleep_duration_by_day,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "data": None}


def _seconds_to_hours_min(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h > 0:
        return f"{h}ч {m}мин"
    return f"{m}мин"


def build_readable_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Превращает сырой ответ fetch_recent_data в удобочитаемую сводку по дням.
    """
    if not raw.get("ok") or not raw.get("stats_by_day"):
        return {"ok": raw.get("ok", False), "error": raw.get("error"), "days": []}

    sleep_by_day = raw.get("sleep_duration_by_day") or {}
    days_summary = []
    for date_str in sorted(raw["stats_by_day"].keys(), reverse=True):
        s = raw["stats_by_day"][date_str]
        steps = s.get("totalSteps")
        goal = s.get("dailyStepGoal")
        steps_str = f"{steps:,}".replace(",", " ") if steps is not None else "—"
        goal_str = f"{goal:,}".replace(",", " ") if goal is not None else "—"
        steps_with_goal = f"{steps_str} / {goal_str}" if (steps is not None and goal is not None) else steps_str

        # Длительность сна: приоритет — get_sleep_data (как на странице Sleep), иначе — из get_stats
        sleep_sec = sleep_by_day.get(date_str) or s.get("sleepingSeconds") or s.get("measurableAsleepDuration")
        days_summary.append({
            "date": date_str,
            "steps": steps_with_goal,
            "steps_value": steps,
            "step_goal": goal,
            "sleep": _seconds_to_hours_min(sleep_sec),
            "sleep_seconds": sleep_sec,
            "calories_total": int(s["totalKilocalories"]) if s.get("totalKilocalories") is not None else None,
            "calories_active": int(s["activeKilocalories"]) if s.get("activeKilocalories") is not None else None,
            "distance_km": round((s.get("totalDistanceMeters") or 0) / 1000, 2) if s.get("totalDistanceMeters") else None,
            "stress": s.get("stressQualifier") or "—",
            "stress_avg": s.get("averageStressLevel"),
            "body_battery": f"{s.get('bodyBatteryAtWakeTime', '—')} → {s.get('bodyBatteryMostRecentValue', '—')}" if s.get("bodyBatteryAtWakeTime") is not None else "—",
            "body_battery_wake": s.get("bodyBatteryAtWakeTime"),
            "body_battery_end": s.get("bodyBatteryMostRecentValue"),
            "resting_hr": s.get("restingHeartRate"),
            "floors": int(s.get("floorsAscended") or 0) if s.get("floorsAscended") is not None else None,
        })

    return {
        "ok": True,
        "from": raw.get("from"),
        "to": raw.get("to"),
        "activities_count": len(raw.get("activities") or []),
        "days": days_summary,
    }
