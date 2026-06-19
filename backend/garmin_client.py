"""
Клиент для загрузки данных из Garmin Connect (активность, сон, статистика).
Учётные данные берутся из переменных окружения GARMIN_EMAIL и GARMIN_PASSWORD.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Ленивый импорт: garminconnect ставится опционально для окружений без Garmin
try:
    from garminconnect import Garmin
    from garminconnect import (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )
except ImportError:
    Garmin = None  # type: ignore
    GarminConnectAuthenticationError = type("GarminConnectAuthenticationError", (Exception,), {})  # type: ignore
    GarminConnectConnectionError = type("GarminConnectConnectionError", (Exception,), {})  # type: ignore
    GarminConnectTooManyRequestsError = type("GarminConnectTooManyRequestsError", (Exception,), {})  # type: ignore


def _garmin_tokenstore_dir() -> Path:
    """Каталог с oauth1/oauth2 JSON (переменная GARMINTOKENS или .garmin-tokens в корне проекта)."""
    custom = os.environ.get("GARMINTOKENS", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / ".garmin-tokens").resolve()


def _garmin_tokens_on_disk(store: Path) -> bool:
    """Файлы сессии: garminconnect 0.3+ (garmin_tokens.json) или старый garth (oauth*.json)."""
    if not store.is_dir():
        return False
    if (store / "garmin_tokens.json").is_file():
        return True
    return (store / "oauth1_token.json").is_file() and (store / "oauth2_token.json").is_file()


def _err_detail(exc: BaseException, max_len: int = 800) -> str:
    s = str(exc).strip()
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


def get_client() -> tuple[Any | None, str | None, str | None]:
    """
    Создаёт и логинит клиент Garmin.
    Успех: (api, None, None). Ошибка: (None, код, detail) — detail для отладки (текст исключения).

    Приоритет авторизации:
    1. GARMINTOKENS_BASE64 — готовый токен (base64-строка от garth). Логин без SSO,
       подходит для Cloud Run (токен берётся из Secret Manager). Cloudflare/429 не задействуются.
    2. GARMIN_EMAIL / GARMIN_PASSWORD — полноценный SSO-вход (может упереться в 429).
    """
    if Garmin is None:
        return None, "garmin_sdk_missing", "garminconnect not installed"

    token_b64 = os.environ.get("GARMINTOKENS_BASE64", "").strip()
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()

    if not token_b64 and (not email or not password):
        return None, "garmin_not_configured", "no GARMINTOKENS_BASE64 and no GARMIN_EMAIL/PASSWORD"

    try:
        api = Garmin(email or None, password or None)
        if token_b64:
            # login() трактует строку длиной > 512 как сами токены (client.loads).
            api.login(tokenstore=token_b64)
        else:
            store = _garmin_tokenstore_dir()
            try:
                store.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            # garminconnect 0.3+ сам грузит/сохраняет garmin_tokens.json в каталоге.
            api.login(tokenstore=str(store))
            # garminconnect < 0.3 (garth): дополнительно сохранить oauth-файлы.
            if hasattr(api, "garth") and api.garth is not None:
                try:
                    api.garth.dump(str(store))
                except OSError:
                    pass
        return api, None, None
    except GarminConnectTooManyRequestsError as e:
        return None, "garmin_rate_limited", _err_detail(e)
    except GarminConnectAuthenticationError as e:
        # garminconnect 0.3 иногда заворачивает 429 в AuthenticationError (см. portal login).
        err_low = str(e).lower()
        if "429" in err_low or "rate limit" in err_low:
            return None, "garmin_rate_limited", _err_detail(e)
        # Неверный пароль, MFA без prompt_mfa, пустой профиль и т.д.
        return None, "garmin_auth_failed", _err_detail(e)
    except GarminConnectConnectionError as e:
        err_low = str(e).lower()
        if "429" in err_low or "too many requests" in err_low:
            return None, "garmin_rate_limited", _err_detail(e)
        return None, "garmin_login_failed", _err_detail(e)
    except Exception as e:
        return None, "garmin_login_failed", _err_detail(e)


def fetch_recent_data(days: int = 1) -> dict[str, Any]:
    """
    Загружает данные за последние days дней: активность и статистика.
    Возвращает словарь, готовый для передачи в агента/отчёт.
    """
    api, client_err, client_detail = get_client()
    if api is None:
        out: dict[str, Any] = {"ok": False, "error": client_err, "data": None}
        if client_detail:
            out["detail"] = client_detail
        if client_err == "garmin_rate_limited":
            out["hint"] = (
                "Garmin SSO временно ограничил входы (429). Подождите 15–60 мин, "
                "не жмите Execute подряд. После первого успешного входа сессия кэшируется в .garmin-tokens."
            )
        if client_err == "garmin_auth_failed" and client_detail and "MFA" in client_detail:
            out["hint"] = (
                "Аккаунт требует MFA. Для API без интерактива временно отключите MFA в настройках Garmin "
                "или используйте официальный demo.py с prompt_mfa. См. README garminconnect."
            )
        return out

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


# Список методов API, возвращающих дневные метрики (один аргумент — date YYYY-MM-DD).
# Все результаты попадут в metrics_by_day[date][key] для передачи в Gemini.
_DAILY_METRIC_METHODS = (
    "get_stats",
    "get_user_summary",
    "get_steps_data",
    "get_floors",
    "get_heart_rates",
    "get_sleep_data",
    "get_body_composition",
    "get_hydration_data",
    "get_respiration_data",
    "get_spo2_data",
    "get_intensity_minutes_data",
    "get_all_day_stress",
    "get_stress_data",
    "get_rhr_day",
    "get_hrv_data",
    "get_training_readiness",
    "get_morning_training_readiness",
    "get_training_status",
    "get_fitnessage_data",
    "get_lifestyle_logging_data",
    "get_daily_weigh_ins",
    "get_body_battery",
    # Дополнительные дневные
    "get_stats_and_body",
    "get_body_battery_events",
    "get_max_metrics",
    "get_all_day_events",
    "get_activities_fordate",
    "get_menstrual_data_for_date",
)

# Методы с диапазоном (start, end): вызываются один раз за период, результат в range_metrics.
_RANGE_METRIC_METHODS = (
    "get_daily_steps",
    "get_weigh_ins",
    "get_blood_pressure",
    "get_endurance_score",
    "get_hill_score",
    "get_race_predictions",
    "get_weekly_intensity_minutes",
)

# Методы без даты: один вызов, результат в global_metrics.
_GLOBAL_METRIC_METHODS = (
    "get_user_profile",
    "get_goals",
    "get_personal_record",
)


def _client_error_payload(client_err: str | None, client_detail: str | None) -> dict[str, Any]:
    """Единый формат ответа об ошибке логина (используется во всех fetch-функциях)."""
    out: dict[str, Any] = {"ok": False, "error": client_err, "metrics_by_day": None}
    if client_detail:
        out["detail"] = client_detail
    if client_err == "garmin_rate_limited":
        out["hint"] = (
            "Garmin SSO временно ограничил входы (429). Подождите 15–60 мин, "
            "не жмите Execute подряд. После первого успешного входа сессия кэшируется в .garmin-tokens."
        )
    if client_err == "garmin_auth_failed" and client_detail and "MFA" in client_detail:
        out["hint"] = (
            "Аккаунт требует MFA. Для API без интерактива временно отключите MFA в настройках Garmin "
            "или используйте официальный demo.py с prompt_mfa. См. README garminconnect."
        )
    return out


def _collect_metrics_range(api: Any, start: Any, end: Any) -> dict[str, Any]:
    """Собирает все метрики Garmin за диапазон [start; end] включительно (start, end — date)."""
    day_count = (end - start).days + 1

    # Активности за период
    activity_list: list[dict[str, Any]] = []
    try:
        activities = api.get_activities_by_date(
            startdate=start.isoformat(),
            enddate=end.isoformat(),
        )
        if isinstance(activities, list):
            for a in activities:
                activity_list.append(dict(a))  # полный объект для Gemini
    except Exception:
        pass

    # Все дневные метрики по дням
    metrics_by_day: dict[str, dict[str, Any]] = {}
    for d in range(day_count):
        day = start + timedelta(days=d)
        day_str = day.isoformat()
        metrics_by_day[day_str] = {}

        for method_name in _DAILY_METRIC_METHODS:
            method = getattr(api, method_name, None)
            if not callable(method):
                continue
            try:
                # методы с (startdate, enddate) вызываем с одним днём
                if method_name in ("get_body_composition", "get_body_battery"):
                    result = method(day_str, day_str)
                else:
                    result = method(day_str)
                if result is not None:
                    key = method_name.replace("get_", "", 1)
                    metrics_by_day[day_str][key] = result
            except Exception:
                pass

    # Методы с диапазоном дат: один вызов на весь период
    start_str = start.isoformat()
    end_str = end.isoformat()
    range_metrics: dict[str, Any] = {}
    for method_name in _RANGE_METRIC_METHODS:
        method = getattr(api, method_name, None)
        if not callable(method):
            continue
        try:
            if method_name == "get_race_predictions":
                result = method(startdate=start_str, enddate=end_str)
            else:
                result = method(start_str, end_str)
            if result is not None:
                key = method_name.replace("get_", "", 1)
                range_metrics[key] = result
        except Exception:
            pass

    # Глобальные методы (без даты): один вызов
    global_metrics: dict[str, Any] = {}
    for method_name in _GLOBAL_METRIC_METHODS:
        method = getattr(api, method_name, None)
        if not callable(method):
            continue
        try:
            if method_name == "get_goals":
                result = method("active", 0, 30)
            else:
                result = method()
            if result is not None:
                key = method_name.replace("get_", "", 1)
                global_metrics[key] = result
        except Exception:
            pass

    # Лактатный порог (спец. сигнатура: latest=True)
    try:
        lt = getattr(api, "get_lactate_threshold", None)
        if callable(lt):
            result = lt(latest=True)
            if result is not None:
                global_metrics["lactate_threshold"] = result
    except Exception:
        pass

    return {
        "ok": True,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "activities": activity_list,
        "metrics_by_day": metrics_by_day,
        "range_metrics": range_metrics,
        "global_metrics": global_metrics,
    }


def fetch_all_metrics(days: int = 7) -> dict[str, Any]:
    """
    Загружает все доступные метрики Garmin за последние days дней.
    Для каждого дня вызываются все дневные API (stats, sleep, heart rate, stress, body battery,
    hydration, respiration, SpO2, HRV, training readiness и т.д.).
    Результат готов для передачи в Gemini как полный контекст.
    """
    api, client_err, client_detail = get_client()
    if api is None:
        return _client_error_payload(client_err, client_detail)
    try:
        end = datetime.now().date()
        start = end - timedelta(days=days)
        return _collect_metrics_range(api, start, end)
    except Exception as e:
        return {"ok": False, "error": str(e), "metrics_by_day": None}


def _report_timezone() -> ZoneInfo:
    tz_name = os.environ.get("REPORT_TIMEZONE", "Europe/Dublin").strip() or "Europe/Dublin"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Europe/Dublin")


def _report_calendar_day() -> Any:
    """Календарный «сегодня» в REPORT_TIMEZONE (как file_stem и папки Meals)."""
    return datetime.now(_report_timezone()).date()


def _default_report_day() -> Any:
    """
    День утреннего отчёта по умолчанию (когда day= не передан) — дата пробуждения (сегодня).
    REPORT_DAY_OFFSET (например -1) сдвигает от «сегодня» в REPORT_TIMEZONE.
    """
    today = _report_calendar_day()
    env_offset = os.environ.get("REPORT_DAY_OFFSET", "").strip()
    if env_offset:
        try:
            return today + timedelta(days=int(env_offset))
        except ValueError:
            pass
    return today


def fetch_daily_metrics(day: str | None = None) -> dict[str, Any]:
    """
    Метрики для утреннего отчёта за дату пробуждения D (report_day).
    Загружает D−1 и D: активность/стресс за вчера, sleep_data за D — прошедшая ночь.
    """
    api, client_err, client_detail = get_client()
    if api is None:
        return _client_error_payload(client_err, client_detail)
    try:
        if day:
            report_day = datetime.fromisoformat(day).date()
        else:
            report_day = _default_report_day()
        activity_day = report_day - timedelta(days=1)
        payload = _collect_metrics_range(api, activity_day, report_day)
        payload["report_day"] = report_day.isoformat()
        payload["activity_day"] = activity_day.isoformat()
        payload["food_day"] = activity_day.isoformat()
        payload["sleep_day"] = report_day.isoformat()
        return payload
    except Exception as e:
        return {"ok": False, "error": str(e), "metrics_by_day": None}


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


def summary_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """
    Строит удобочитаемую сводку по дням из результата fetch_all_metrics / fetch_daily_metrics
    (переиспользует build_readable_summary). Удобно для таблицы в email-отчёте.
    """
    if not metrics.get("ok"):
        return {"ok": False, "error": metrics.get("error"), "days": []}
    by_day = metrics.get("metrics_by_day") or {}
    stats_by_day: dict[str, Any] = {}
    sleep_duration_by_day: dict[str, int] = {}
    for date_str, day in by_day.items():
        stats = day.get("stats")
        if isinstance(stats, dict):
            stats_by_day[date_str] = stats
        sleep_data = day.get("sleep_data")
        if isinstance(sleep_data, dict):
            dto = sleep_data.get("dailySleepDTO") or sleep_data
            sec = dto.get("sleepTimeSeconds") if isinstance(dto, dict) else None
            if sec is not None:
                sleep_duration_by_day[date_str] = int(sec)
    raw = {
        "ok": True,
        "from": metrics.get("from"),
        "to": metrics.get("to"),
        "activities": metrics.get("activities") or [],
        "stats_by_day": stats_by_day,
        "sleep_duration_by_day": sleep_duration_by_day,
    }
    return build_readable_summary(raw)
