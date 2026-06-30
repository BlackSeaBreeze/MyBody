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


def _call_daily_metric(api: Any, method_name: str, day_str: str) -> Any:
    method = getattr(api, method_name, None)
    if not callable(method):
        return None
    try:
        if method_name in ("get_body_composition", "get_body_battery"):
            return method(day_str, day_str)
        return method(day_str)
    except Exception:
        return None


def _collect_global_metrics(api: Any) -> dict[str, Any]:
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
    try:
        lt = getattr(api, "get_lactate_threshold", None)
        if callable(lt):
            result = lt(latest=True)
            if result is not None:
                global_metrics["lactate_threshold"] = result
    except Exception:
        pass
    return global_metrics


def _collect_morning_report_range(api: Any, activity_day: Any, sleep_day: Any) -> dict[str, Any]:
    """
    Утренний отчёт: полные дневные метрики за activity_day (D−1), без sleep_data;
    за sleep_day (D) — только sleep_data (ночь → пробуждение).
    Без range_metrics — меньше объём для Gemini.
    """
    activity_str = activity_day.isoformat()
    sleep_str = sleep_day.isoformat()

    metrics_by_day: dict[str, dict[str, Any]] = {activity_str: {}, sleep_str: {}}
    for method_name in _DAILY_METRIC_METHODS:
        if method_name == "get_sleep_data":
            continue
        result = _call_daily_metric(api, method_name, activity_str)
        if result is not None:
            metrics_by_day[activity_str][method_name.replace("get_", "", 1)] = result

    sleep_result = _call_daily_metric(api, "get_sleep_data", sleep_str)
    if sleep_result is not None:
        metrics_by_day[sleep_str]["sleep_data"] = sleep_result

    activity_list: list[dict[str, Any]] = []
    try:
        activities = api.get_activities_by_date(startdate=activity_str, enddate=activity_str)
        if isinstance(activities, list):
            for a in activities:
                activity_list.append(dict(a))
    except Exception:
        pass

    return {
        "ok": True,
        "from": activity_str,
        "to": sleep_str,
        "activities": activity_list,
        "metrics_by_day": metrics_by_day,
        "range_metrics": {},
        "global_metrics": _collect_global_metrics(api),
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
        payload = _collect_morning_report_range(api, activity_day, report_day)
        payload["report_day"] = report_day.isoformat()
        payload["activity_day"] = activity_day.isoformat()
        payload["food_day"] = activity_day.isoformat()
        payload["sleep_day"] = report_day.isoformat()
        payload["report_context"] = {
            "report_day": report_day.isoformat(),
            "activity_day": activity_day.isoformat(),
            "sleep_day": report_day.isoformat(),
            "food_day": activity_day.isoformat(),
            "note": (
                "Утренний отчёт: активность за activity_day; сон прошедшей ночи — "
                "только sleep_report / sleep_data за sleep_day (дата пробуждения)."
            ),
        }
        payload["sleep_report"] = extract_sleep_report(payload)
        payload["metrics_by_day"] = _morning_report_metrics_by_day(payload)
        return payload
    except Exception as e:
        return {"ok": False, "error": str(e), "metrics_by_day": None}


def _sec_to_min(seconds: Any) -> int | None:
    if seconds is None:
        return None
    try:
        return int(round(int(seconds) / 60))
    except (TypeError, ValueError):
        return None


def _sleep_score_value(scores: Any, key: str) -> Any:
    if not isinstance(scores, dict):
        return None
    item = scores.get(key)
    if isinstance(item, dict):
        return item.get("value") if item.get("value") is not None else item.get("qualifierKey")
    return item


def _ts_label(ts: Any) -> str | None:
    if ts is None:
        return None
    try:
        ms = int(ts)
        if ms > 10_000_000_000:
            ms //= 1000
        return datetime.fromtimestamp(ms).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return None


def _dig(dto: dict[str, Any], *keys: str) -> Any:
    """Первое непустое значение по списку ключей."""
    for key in keys:
        if not isinstance(dto, dict):
            continue
        val = dto.get(key)
        if val is not None and val != "":
            return val
    return None


def _sleep_payload_parts(sleep_data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Корень ответа get_sleep_data и вложенный dailySleepDTO."""
    if not isinstance(sleep_data, dict):
        return {}, {}
    nested = sleep_data.get("dailySleepDTO")
    if isinstance(nested, dict):
        return sleep_data, nested
    return sleep_data, sleep_data


def _sleep_quality_label(dto: dict[str, Any], scores: dict[str, Any]) -> str | None:
    """Качество сна: тип или квалификаторы из sleepScores (stress/quality/overall)."""
    explicit = dto.get("sleepQualityType") or dto.get("sleepQuality")
    if explicit:
        return str(explicit)
    parts: list[str] = []
    for key, prefix in (
        ("overall", "overall"),
        ("quality", "quality"),
        ("stress", "stress"),
        ("awakeCount", "awake"),
    ):
        val = _sleep_score_value(scores, key)
        if val is not None:
            parts.append(f"{prefix}: {val}")
    return " · ".join(parts) if parts else None


def extract_vo2_max(max_metrics: Any) -> dict[str, Any]:
    """
    VO2 max из get_max_metrics (список блоков generic / cycling / running).
    """
    out: dict[str, Any] = {"found": False, "generic": None, "cycling": None, "display": None}
    if max_metrics is None:
        return out
    items = max_metrics if isinstance(max_metrics, list) else [max_metrics]
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for sport_key, sport_label in (("generic", "бег"), ("running", "бег"), ("cycling", "вело")):
            block = item.get(sport_key)
            if not isinstance(block, dict):
                continue
            value = block.get("vo2MaxPreciseValue")
            if value is None:
                value = block.get("vo2MaxValue")
            if value is None:
                continue
            store_key = "generic" if sport_key == "running" else sport_key
            if out.get(store_key) is None:
                out[store_key] = value
                labels.append(f"{sport_label} {value}")
    if labels:
        out["found"] = True
        # Уникальные подписи (generic и running могут дублировать «бег»)
        seen: set[str] = set()
        unique: list[str] = []
        for label in labels:
            if label not in seen:
                seen.add(label)
                unique.append(label)
        out["display"] = " · ".join(unique)
    return out


def extract_vo2_max_from_day_metrics(day: dict[str, Any]) -> dict[str, Any]:
    """VO2 max за день: max_metrics, иначе mostRecentVO2Max из training_status."""
    vo2 = extract_vo2_max(day.get("max_metrics"))
    if vo2.get("found"):
        return vo2
    ts = day.get("training_status")
    if not isinstance(ts, dict):
        return vo2
    recent = ts.get("mostRecentVO2Max")
    if not isinstance(recent, dict):
        return vo2
    return extract_vo2_max(recent)


def extract_sleep_report(metrics: dict[str, Any]) -> dict[str, Any]:
    """
    Структурированные метрики одной ночи (sleep_data на sleep_day = дата пробуждения).
    Источник для промптов и трендов в архиве.
    """
    sleep_day = str(metrics.get("sleep_day") or metrics.get("report_day") or "")
    out: dict[str, Any] = {"ok": True, "found": False, "sleep_day": sleep_day}
    if not sleep_day:
        return out

    by_day = metrics.get("metrics_by_day") or {}
    day = by_day.get(sleep_day) or {}
    sleep_data = day.get("sleep_data")
    if not isinstance(sleep_data, dict):
        return out

    root, dto = _sleep_payload_parts(sleep_data)
    if not isinstance(dto, dict):
        return out

    scores = dto.get("sleepScores") or {}
    restless = _dig(
        root,
        "restlessMomentsCount",
        "restlessMomentCount",
    )
    if restless is None:
        restless = _dig(
            dto,
            "restlessMomentsCount",
            "restlessMomentCount",
        )
    avg_hr = _dig(
        dto,
        "avgHeartRate",
        "averageSleepHeartRate",
        "sleepHeartRate",
        "restingHeartRate",
    )
    if avg_hr is None:
        avg_hr = _dig(root, "avgHeartRate", "averageSleepHeartRate")

    out.update(
        {
            "found": True,
            "night_label": f"ночь → {sleep_day}",
            "total_sleep_min": _sec_to_min(dto.get("sleepTimeSeconds")),
            "total_sleep_hm": format_sleep_duration(seconds=dto.get("sleepTimeSeconds")),
            "deep_sleep_min": _sec_to_min(dto.get("deepSleepSeconds")),
            "light_sleep_min": _sec_to_min(dto.get("lightSleepSeconds")),
            "rem_sleep_min": _sec_to_min(dto.get("remSleepSeconds")),
            "awake_min": _sec_to_min(dto.get("awakeSleepSeconds")),
            "restless_moments": restless,
            "avg_sleep_stress": dto.get("avgSleepStress"),
            "avg_hr_sleep": avg_hr,
            "min_hr_sleep": dto.get("lowestHeartRate") or dto.get("lowestHeartRateDuringSleep"),
            "max_hr_sleep": dto.get("highestHeartRate") or dto.get("maxHeartRate"),
            "avg_respiration": dto.get("averageRespiration") or dto.get("avgRespirationValue"),
            "avg_spo2": dto.get("averageSpO2") or dto.get("averageSpO2Value"),
            "lowest_spo2": dto.get("lowestSpO2") or dto.get("lowestSpO2Value"),
            "sleep_score_overall": _sleep_score_value(scores, "overall"),
            "sleep_score_quality": _sleep_score_value(scores, "quality"),
            "sleep_score_recovery": _sleep_score_value(scores, "recovery"),
            "sleep_score_duration": _sleep_score_value(scores, "duration"),
            "sleep_score_stress": _sleep_score_value(scores, "stress"),
            "sleep_score_awake": _sleep_score_value(scores, "awakeCount"),
            "sleep_quality_type": dto.get("sleepQualityType") or dto.get("sleepQuality"),
            "sleep_quality_label": _sleep_quality_label(dto, scores),
            "sleep_feedback": dto.get("sleepFeedback") or dto.get("sleepScoreFeedback"),
            "validation": dto.get("validation") or dto.get("sleepValidation"),
            "bedtime": _ts_label(dto.get("sleepStartTimestampLocal") or dto.get("sleepStartTimestampGMT")),
            "wake_time": _ts_label(dto.get("sleepEndTimestampLocal") or dto.get("sleepEndTimestampGMT")),
        }
    )
    return out


def _morning_report_metrics_by_day(metrics: dict[str, Any]) -> dict[str, Any]:
    """Для Gemini: активность за D−1 без sleep_data; сон только на sleep_day."""
    by_day = dict(metrics.get("metrics_by_day") or {})
    activity_day = str(metrics.get("activity_day") or "")
    sleep_day = str(metrics.get("sleep_day") or "")
    filtered: dict[str, Any] = {}
    if activity_day and activity_day in by_day:
        day = dict(by_day[activity_day])
        day.pop("sleep_data", None)
        filtered[activity_day] = day
    if sleep_day and sleep_day in by_day:
        day = by_day[sleep_day]
        sleep_data = day.get("sleep_data") if isinstance(day, dict) else None
        if sleep_data is not None:
            filtered[sleep_day] = {"sleep_data": sleep_data}
    return filtered


def format_sleep_duration(
    seconds: int | float | None = None,
    *,
    minutes: int | float | None = None,
) -> str:
    """
    Длительность сна для UI и отчётов.
    > 60 мин → ч:мм (например 7:05); иначе «N мин».
    """
    if seconds is not None:
        try:
            total_min = int(round(int(seconds) / 60))
        except (TypeError, ValueError):
            return "—"
    elif minutes is not None:
        try:
            total_min = int(round(float(minutes)))
        except (TypeError, ValueError):
            return "—"
    else:
        return "—"
    if total_min <= 0:
        return "—"
    if total_min > 60:
        h, m = divmod(total_min, 60)
        return f"{h}:{m:02d}"
    return f"{total_min} мин"


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
            "sleep": format_sleep_duration(seconds=sleep_sec),
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
    Сводка для email: для утреннего отчёта — активность за D−1 и сон одной ночи (sleep_day).
    """
    if not metrics.get("ok"):
        return {"ok": False, "error": metrics.get("error"), "days": []}
    if metrics.get("report_day"):
        return build_morning_report_summary(metrics)

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


def build_morning_report_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    """Email-таблица: одна строка активности (D−1) + блок сна одной ночи (sleep_day)."""
    activity_day = str(metrics.get("activity_day") or "")
    sleep_report = metrics.get("sleep_report") or extract_sleep_report(metrics)
    by_day = metrics.get("metrics_by_day") or {}

    activity_row: dict[str, Any] = {"date": activity_day, "label": f"Активность ({activity_day})"}
    vo2_max: dict[str, Any] = {"found": False}
    if activity_day and activity_day in by_day:
        day_metrics = by_day[activity_day] or {}
        vo2_max = extract_vo2_max_from_day_metrics(day_metrics)
        stats = day_metrics.get("stats")
        if isinstance(stats, dict):
            partial = build_readable_summary(
                {
                    "ok": True,
                    "stats_by_day": {activity_day: stats},
                    "sleep_duration_by_day": {},
                    "activities": [],
                }
            )
            days = partial.get("days") or []
            if days:
                activity_row = {**days[0], "label": f"Активность ({activity_day})", "sleep": "—"}

    return {
        "ok": True,
        "mode": "morning_report",
        "activity_day": activity_day,
        "sleep_day": sleep_report.get("sleep_day"),
        "activity": activity_row,
        "sleep": sleep_report,
        "vo2_max": vo2_max,
        "days": [activity_row],
    }
