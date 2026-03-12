"""
Клиент для анализа данных через Google Gemini.
Использует GEMINI_API_KEY (Google AI Studio, https://aistudio.google.com/apikey).
Данные Garmin передаются в модель для получения рекомендаций по здоровью и активности.
"""
from __future__ import annotations

import json
import os
from typing import Any

try:
    import google.generativeai as genai
except ImportError:
    genai = None  # type: ignore


def is_configured() -> bool:
    """Проверка: задан ли API-ключ Gemini."""
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _metrics_to_text(metrics: dict[str, Any]) -> str:
    """Превращает результат fetch_all_metrics в компактный текст для промпта."""
    if not metrics.get("ok"):
        return json.dumps({"error": metrics.get("error", "unknown")}, ensure_ascii=False, indent=2)

    parts = [
        f"Период: с {metrics.get('from', '')} по {metrics.get('to', '')}.",
        "",
        "=== Активности ===",
        json.dumps(metrics.get("activities") or [], ensure_ascii=False, indent=2),
        "",
        "=== Метрики по дням (metrics_by_day) ===",
    ]
    by_day = metrics.get("metrics_by_day") or {}
    for date_str in sorted(by_day.keys(), reverse=True):
        day_data = by_day[date_str]
        flat = {}
        for k, v in day_data.items():
            if isinstance(v, (dict, list)) and v:
                flat[k] = v
            elif v is not None:
                flat[k] = v
        if flat:
            parts.append(f"\n--- {date_str} ---")
            parts.append(json.dumps(flat, ensure_ascii=False, default=str))
    parts.extend([
        "",
        "=== Метрики за период (range_metrics) ===",
        json.dumps(metrics.get("range_metrics") or {}, ensure_ascii=False, indent=2, default=str),
        "",
        "=== Глобальные метрики (цели, профиль и т.д.) ===",
        json.dumps(metrics.get("global_metrics") or {}, ensure_ascii=False, indent=2, default=str),
    ])
    return "\n".join(parts)


SYSTEM_PROMPT = """Ты — персональный помощник по здоровью и активности. Тебе передают сырые данные из Garmin Connect за последние дни.

Задачи:
1. Кратко проанализировать динамику: сон, шаги, стресс, Body Battery, пульс покоя, тренировки.
2. Отметить позитивные тенденции и возможные зоны внимания (недосып, высокий стресс, низкое восстановление).
3. Дать 2–4 конкретные рекомендации на ближайшие дни (режим сна, нагрузка, отдых).

Формат ответа: на русском языке, структурированно (короткие абзацы или списки), без лишнего вступления. Не придумывай данные — опирайся только на переданные метрики."""


def analyze_garmin_metrics(metrics: dict[str, Any], model: str = "gemini-2.0-flash") -> dict[str, Any]:
    """
    Отправляет данные Garmin в Gemini и возвращает анализ и рекомендации.

    :param metrics: результат garmin_client.fetch_all_metrics(days=...)
    :param model: имя модели. По умолчанию gemini-2.0-flash (актуальный ID в API v1beta).
                  Варианты: gemini-2.5-flash, gemini-1.5-pro. Список: GET https://generativelanguage.googleapis.com/v1beta/models?key=API_KEY
    :return: {"ok": True, "analysis": "текст от модели"} или {"ok": False, "error": "..."}
    """
    if genai is None:
        return {"ok": False, "error": "gemini_sdk_not_installed", "analysis": None}
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "gemini_not_configured", "analysis": None}

    if not metrics.get("ok"):
        return {
            "ok": False,
            "error": metrics.get("error", "garmin_data_unavailable"),
            "analysis": None,
        }

    text_context = _metrics_to_text(metrics)
    user_content = (
        "Ниже данные из Garmin Connect за указанный период. "
        "Проанализируй их и дай краткий отчёт с рекомендациями.\n\n"
        + text_context
    )

    try:
        genai.configure(api_key=api_key)
        gemini_model = genai.GenerativeModel(
            model,
            system_instruction=SYSTEM_PROMPT,
            generation_config=genai.types.GenerationConfig(
                temperature=0.4,
            ),
        )
        response = gemini_model.generate_content(user_content)
        if not response or not response.text:
            return {"ok": False, "error": "empty_gemini_response", "analysis": None}
        return {"ok": True, "analysis": response.text.strip(), "model": model}
    except Exception as e:
        return {"ok": False, "error": str(e), "analysis": None}
