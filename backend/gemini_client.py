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


SYSTEM_PROMPT_DETAILED = """Ты — медицинско-спортивный аналитик данных Garmin с опытом интерпретации носимых датчиков.
Тебе передают сырые данные за ОДИН день. Твой ответ сохраняется в архив для последующего мета-анализа
за неделю или месяц другой моделью Gemini.

ГЛАВНАЯ ЗАДАЧА: написать экспертный аналитический отчёт — выводы, интерпретации, связи и нюансы.
НЕ переписывай и НЕ дублируй сырые данные, временные ряды и JSON. Цифры включай только как доказательства
к конкретным выводам (например: «пик стресса 87 около 15:00 совпал с…», а не полный список всех значений).

ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (Markdown, все секции заполни; если данных нет — «нет данных»):

## meta
- date: YYYY-MM-DD
- overall_day_assessment: 2–4 предложения — как прошёл день с точки зрения восстановления, нагрузки и рисков
- data_completeness: high/medium/low и что отсутствует для уверенных выводов

## sleep
Экспертная интерпретация сна (sleep_data): качество, фазы, что мешало восстановлению, связь с утренними метриками.
Отметь нюансы (поздний отход, мало deep/REM, высокий ЧСС во сне, SpO2 и т.д.) — только значимое.

## stress_and_hrv
Что означает стресс этого дня физиологически (Garmin стресс = HRV, не эмоции; stress_data, all_day_stress).
Пики, их вероятные причины, связь со сном, нагрузкой, RHR. HRV (hrv_data) vs baseline — что это говорит о восстановлении.

## body_battery_and_recovery
Оценка ресурса дня (body_battery, body_battery_events): старт/финиш, ключевые спады и подъёмы, триггеры, готовность к нагрузке.

## activity_and_load
Баланс активности: шаги, этажи, калории, дистанция — достаточно/мало/перебор.
Каждая тренировка (activities, activities_fordate) — смысл для дня, не таблица полей.
Эффект на восстановление и стресс.

## cardiovascular_and_respiration
RHR (rhr_day), пульс за день (heart_rates), дыхание (respiration_data), SpO2 (spo2_data),
гидратация (hydration_data) — только если влияют на интерпретацию дня.

## body_composition_and_vitals
Вес и состав тела (body_composition, daily_weigh_ins, weigh_ins из range_metrics),
давление (blood_pressure) — только если есть данные; тренд и смысл для дня и восстановления.

## training_performance_context
Training status, readiness (training_readiness, morning_training_readiness, training_status),
минуты интенсивности (intensity_minutes_data, weekly_intensity_minutes),
VO2/endurance/hill/race predictions (max_metrics, endurance_score, hill_score, race_predictions),
лактатный порог и рекорды (lactate_threshold, personal_record из global_metrics) —
только значимое для интерпретации нагрузки, формы и восстановления.

## optional_context
lifestyle_logging_data, menstrual_data_for_date, all_day_events, fitnessage_data, user_summary,
stats_and_body, goals/user_profile — если есть и влияет на выводы; иначе кратко «нет значимых данных».

## day_timeline
Хронология дня связным повествованием: сон → утро → активность → вечер. Ключевые поворотные моменты с цифрами.

## correlations_and_insights
Минимум 5 конкретных выводов вида «когда X, то Y, потому что Z» — с нюансами и оговорками.
Отдельно: неочевидные наблюдения, которые легко пропустить при поверхностном просмотре.

## problems_and_risks
Проблемные зоны с severity (low/medium/high), evidence (ключевые цифры) и практическим смыслом.

## hypotheses
Обоснованные гипотезы о причинах отклонений. Различай «данные показывают» vs «возможно, но не подтверждено».

## recommendations_for_next_days
2–5 конкретных рекомендаций, вытекающих из анализа (сон, нагрузка, отдых, режим).

## data_gaps
Чего не хватает для точного вывода (еда, алкоголь, кофеин, самочувствие и т.д.).

## facts_for_aggregation
Компактные атомарные факты для машинного сведения при недельном/месячном merge — одна строка на факт:
- FACT | category=... | severity=... | summary=... | evidence=...

Правила:
- Язык: русский.
- Просмотри все блоки во входе (activities, metrics_by_day, range_metrics, global_metrics).
  Если блок присутствует — отрази в соответствующей секции выводы или явно укажи «нет значимых отклонений».
- Пиши развёрнуто и содержательно, но каждый абзац — анализ, а не дамп метрик.
- Не придумывай метрики и события, которых нет во входе.
- Стресс Garmin = HRV-метрика, не психология."""


def _call_gemini(
    *,
    metrics: dict[str, Any],
    model: str,
    system_instruction: str,
    user_intro: str,
    temperature: float = 0.4,
    max_output_tokens: int | None = None,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
) -> dict[str, Any]:
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

    text_context, meta = build_prompt_context(metrics, max_input_tokens=max_input_tokens)
    user_content = user_intro + "\n\n" + text_context
    cfg_kwargs: dict[str, Any] = {
        "system_instruction": system_instruction,
        "temperature": temperature,
    }
    if max_output_tokens is not None:
        cfg_kwargs["max_output_tokens"] = max_output_tokens

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=user_content,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )
        if not response or not getattr(response, "text", None):
            return {"ok": False, "error": "empty_gemini_response", "analysis": None, "context": meta}
        return {"ok": True, "analysis": response.text.strip(), "model": model, "context": meta}
    except Exception as e:
        return {"ok": False, "error": str(e), "analysis": None, "context": meta}


DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


def analyze_garmin_metrics(metrics: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """
    Отправляет данные Garmin в Gemini и возвращает анализ и рекомендации.

    :param metrics: результат garmin_client.fetch_all_metrics(days=...)
    :param model: имя модели. По умолчанию GEMINI_MODEL env или gemini-2.5-flash.
    :return: {"ok": True, "analysis": "текст от модели"} или {"ok": False, "error": "..."}
    """
    model = (model or DEFAULT_GEMINI_MODEL).strip()
    return _call_gemini(
        metrics=metrics,
        model=model,
        system_instruction=SYSTEM_PROMPT,
        user_intro=(
            "Ниже данные из Garmin Connect за указанный период. "
            "Проанализируй их и дай краткий отчёт с рекомендациями."
        ),
        temperature=0.4,
    )


def analyze_garmin_metrics_detailed(
    metrics: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    """
    Экспертный архивный анализ за день для GCS archive/.
    Только выводы и интерпретации — без дублирования сырых метрик в ответе.
    """
    model = (model or DEFAULT_GEMINI_MODEL).strip()
    return _call_gemini(
        metrics=metrics,
        model=model,
        system_instruction=SYSTEM_PROMPT_DETAILED,
        user_intro=(
            "Ниже сырые данные Garmin Connect за один день — используй их только как источник для анализа. "
            "Сформируй экспертный отчёт по обязательной структуре: выводы, связи, нюансы, гипотезы. "
            "Не переписывай сырые данные и ряды в ответ. "
            "Этот текст позже объединят с другими днями для анализа трендов за неделю/месяц."
        ),
        temperature=0.25,
        max_output_tokens=8192,
    )
