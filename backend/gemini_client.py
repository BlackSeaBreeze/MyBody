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
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore


def is_configured() -> bool:
    """Проверка: задан ли API-ключ Gemini."""
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


# --- Адаптивная компакция данных под бюджет токенов -------------------------
#
# Идея: не ужимать по жёсткому правилу "N дней = уровень X", а подгонять объём
# под бюджет входных токенов. Если данные влезают (например, 1 день ≈ 183k <
# free tier 250k) — отдаём как есть, без потерь. Если не влезают — прорежаем
# длинные временные ряды (пульс, стресс, дыхание, Body Battery и т.п.),
# СОХРАНЯЯ агрегаты (count/min/max/avg/first/last), чтобы статистическая
# картина не терялась. Чем больше период — тем выше выбранный уровень ужатия.

# Бесплатный лимит Gemini — 250 000 входных токенов/мин на модель.
# Берём с запасом (системный промпт, обёртка, разметка JSON тоже считаются).
DEFAULT_MAX_INPUT_TOKENS = 220_000
# Эмпирически на данных Garmin ~1.45 символа на токен (плотный JSON с числами).
# Берём заниженное значение → меньший бюджет в символах → безопаснее не превысить.
_CHARS_PER_TOKEN = 1.35

# Профили ужатия от точного (0) к агрессивному. На каждом уровне:
#   short_max   — списки короче считаем "не временным рядом", оставляем как есть;
#   sample_n    — сколько точек оставить от длинного числового ряда (0 = только агрегаты);
#   list_keep   — сколько элементов оставить от длинного списка объектов.
_PROFILES: tuple[dict[str, int] | None, ...] = (
    None,                                              # 0: без ужатия
    {"short_max": 60, "sample_n": 40, "list_keep": 10},  # 1: лёгкое прореживание
    {"short_max": 24, "sample_n": 12, "list_keep": 4},   # 2: среднее
    {"short_max": 12, "sample_n": 0, "list_keep": 2},    # 3: только агрегаты
)


def _sample_evenly(items: list[Any], k: int) -> list[Any]:
    """Равномерно прорежает список до k элементов (с сохранением первого и последнего)."""
    n = len(items)
    if k <= 0 or n <= k:
        return items if n <= k else []
    if k == 1:
        return [items[0]]
    step = (n - 1) / (k - 1)
    idxs = sorted({round(i * step) for i in range(k)})
    return [items[i] for i in idxs]


def _numeric_of(item: Any) -> float | None:
    """Достаёт число из элемента ряда: само число либо value из пары [timestamp, value]."""
    if isinstance(item, bool):
        return None
    if isinstance(item, (int, float)):
        return float(item)
    if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], (int, float)) and not isinstance(item[1], bool):
        return float(item[1])
    return None


def _series_stats(items: list[Any]) -> dict[str, Any] | None:
    """Агрегаты по числовому ряду (или ряду пар [ts, value]). None, если ряд не числовой."""
    nums = [n for n in (_numeric_of(x) for x in items) if n is not None]
    if not nums:
        return None
    return {
        "count": len(items),
        "min": round(min(nums), 3),
        "max": round(max(nums), 3),
        "avg": round(sum(nums) / len(nums), 3),
        "first": items[0],
        "last": items[-1],
    }


def _compact(value: Any, profile: dict[str, int]) -> Any:
    """Рекурсивно ужимает структуру: длинные ряды прорежает + сохраняет агрегаты."""
    if isinstance(value, dict):
        return {k: _compact(v, profile) for k, v in value.items()}
    if isinstance(value, list):
        n = len(value)
        if n <= profile["short_max"]:
            return [_compact(v, profile) for v in value]
        stats = _series_stats(value)
        if stats is not None:
            # Числовой временной ряд: агрегаты + (опционально) прореженные точки.
            out: dict[str, Any] = {"_aggregated": stats}
            sample = _sample_evenly(value, profile["sample_n"])
            if sample:
                out["_sampled"] = sample
            return out
        # Список объектов: оставляем несколько + отметку, сколько всего было.
        keep = profile["list_keep"]
        kept = [_compact(v, profile) for v in value[:keep]]
        return {"_total_items": n, "_kept_first": keep, "items": kept}
    return value


def _render_context(metrics: dict[str, Any], note: str = "") -> str:
    """Собирает текстовый контекст из (возможно ужатого) словаря метрик."""
    parts = [f"Период: с {metrics.get('from', '')} по {metrics.get('to', '')}."]
    if note:
        parts.append(note)
    parts += [
        "",
        "=== Активности ===",
        json.dumps(metrics.get("activities") or [], ensure_ascii=False, indent=2, default=str),
        "",
        "=== Метрики по дням (metrics_by_day) ===",
    ]
    by_day = metrics.get("metrics_by_day") or {}
    for date_str in sorted(by_day.keys(), reverse=True):
        day_data = by_day[date_str]
        flat = {k: v for k, v in day_data.items() if v is not None and not (isinstance(v, (dict, list)) and not v)}
        if flat:
            parts.append(f"\n--- {date_str} ---")
            parts.append(json.dumps(flat, ensure_ascii=False, default=str))
    parts += [
        "",
        "=== Метрики за период (range_metrics) ===",
        json.dumps(metrics.get("range_metrics") or {}, ensure_ascii=False, indent=2, default=str),
        "",
        "=== Глобальные метрики (цели, профиль и т.д.) ===",
        json.dumps(metrics.get("global_metrics") or {}, ensure_ascii=False, indent=2, default=str),
    ]
    return "\n".join(parts)


_COMPACTION_NOTE = (
    "Примечание: длинные временные ряды (пульс, стресс, дыхание, Body Battery и т.п.) "
    "прорежены и заменены агрегатами (_aggregated: count/min/max/avg/first/last, "
    "_sampled — прореженные точки). Это сделано для компактности; пропуски в рядах "
    "не означают отсутствие данных — опирайся на агрегаты."
)


def build_prompt_context(
    metrics: dict[str, Any],
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
) -> tuple[str, dict[str, Any]]:
    """
    Готовит текст для промпта с адаптивным ужатием под бюджет токенов.
    Возвращает (text, meta), где meta = {compaction_level, est_tokens, chars, fits_free_tier}.
    """
    if not metrics.get("ok"):
        text = json.dumps({"error": metrics.get("error", "unknown")}, ensure_ascii=False, indent=2)
        return text, {"compaction_level": 0, "est_tokens": len(text) // 3, "chars": len(text)}

    budget_chars = int(max_input_tokens * _CHARS_PER_TOKEN)
    text = ""
    chosen_level = 0
    for level, profile in enumerate(_PROFILES):
        if profile is None:
            data = metrics
            note = ""
        else:
            data = {
                **{k: metrics.get(k) for k in ("from", "to")},
                "activities": _compact(metrics.get("activities") or [], profile),
                "metrics_by_day": _compact(metrics.get("metrics_by_day") or {}, profile),
                "range_metrics": _compact(metrics.get("range_metrics") or {}, profile),
                "global_metrics": _compact(metrics.get("global_metrics") or {}, profile),
            }
            note = _COMPACTION_NOTE
        text = _render_context(data, note)
        chosen_level = level
        if len(text) <= budget_chars:
            break

    # Подстраховка: если даже на максимальном уровне всё ещё много — жёстко обрезаем.
    if len(text) > budget_chars:
        text = text[:budget_chars] + "\n…(контекст обрезан по лимиту токенов)…"

    meta = {
        "compaction_level": chosen_level,
        "chars": len(text),
        "est_tokens": int(len(text) / _CHARS_PER_TOKEN),
        "fits_free_tier": int(len(text) / _CHARS_PER_TOKEN) <= 250_000,
    }
    return text, meta


def _metrics_to_text(metrics: dict[str, Any], max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS) -> str:
    """Текст для промпта с адаптивным ужатием (обёртка над build_prompt_context)."""
    text, _ = build_prompt_context(metrics, max_input_tokens=max_input_tokens)
    return text


SYSTEM_PROMPT = """Ты — персональный помощник по здоровью и активности. Тебе передают сырые данные из Garmin Connect за последние дни.

Задачи:
1. Кратко проанализировать динамику: сон, шаги, стресс, Body Battery, пульс покоя, тренировки.
2. Отметить позитивные тенденции и возможные зоны внимания (недосып, высокий стресс, низкое восстановление).
3. Дать 2–4 конкретные рекомендации на ближайшие дни (режим сна, нагрузка, отдых).

ОСОБОЕ ВНИМАНИЕ — стресс и другие проблемные показатели:
- Garmin вычисляет «стресс» из вариабельности сердечного ритма (HRV), а не из эмоций. Поэтому высокий стресс часто отражает ФИЗИОЛОГИЮ, а не психологическую нагрузку. Если по жизни сильного стресса нет, ищи телесные причины.
- Для каждого дня/периода с повышенным стрессом постарайся ОБЪЯСНИТЬ ПРИЧИНУ, сопоставив стресс с другими метриками этого же и предыдущего дня: качество и длительность сна (особенно мало глубокого/REM), низкий HRV, повышенный пульс покоя, плохое восстановление Body Battery (низкий уровень при пробуждении), поздние или интенсивные тренировки, высокая ЧСС во сне, нарушения дыхания/SpO2, нерегулярный режим, недобор шагов/активности.
- Частые физиологические причины «беспричинного» высокого стресса: недосып и поздний отход ко сну, перетренированность/недовосстановление, алкоголь и поздний приём пищи, обезвоживание, болезнь/воспаление (рост RHR и снижение HRV), кофеин во второй половине дня. Если данные намекают на одну из них — прямо укажи это как гипотезу.
- Выдели КОНКРЕТНЫЕ дни и временные интервалы с пиками стресса и рядом приведи цифры коррелирующих показателей, чтобы вывод был доказательным, а не общим.
- Отметь, каких данных не хватает, чтобы точнее установить причину (например, время и состав еды, алкоголь, кофеин, субъективное самочувствие).

Формат ответа: на русском языке, структурированно (короткие абзацы или списки), без лишнего вступления. Не придумывай данные — опирайся только на переданные метрики; формулируй причинно-следственные связи как обоснованные гипотезы, а не как факты."""


def analyze_garmin_metrics(metrics: dict[str, Any], model: str = "gemini-2.5-flash") -> dict[str, Any]:
    """
    Отправляет данные Garmin в Gemini и возвращает анализ и рекомендации.

    :param metrics: результат garmin_client.fetch_all_metrics(days=...)
    :param model: имя модели. По умолчанию gemini-2.5-flash (GA, стабильная).
                  Варианты: gemini-3-flash-preview / gemini-3.1-pro-preview (новее, но preview
                  бывает перегружен — 503), gemini-2.0-flash.
    :return: {"ok": True, "analysis": "текст от модели"} или {"ok": False, "error": "..."}
    """
    if genai is None or types is None:
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

    text_context, meta = build_prompt_context(metrics)
    user_content = (
        "Ниже данные из Garmin Connect за указанный период. "
        "Проанализируй их и дай краткий отчёт с рекомендациями.\n\n"
        + text_context
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.4,
            ),
        )
        if not response or not getattr(response, "text", None):
            return {"ok": False, "error": "empty_gemini_response", "analysis": None, "context": meta}
        return {"ok": True, "analysis": response.text.strip(), "model": model, "context": meta}
    except Exception as e:
        return {"ok": False, "error": str(e), "analysis": None, "context": meta}
