"""
Клиент для анализа данных через Google Gemini.
Использует GEMINI_API_KEY (Google AI Studio, https://aistudio.google.com/apikey).
Данные Garmin передаются в модель для получения рекомендаций по здоровью и активности.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from backend import drive_client

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore

logger = logging.getLogger(__name__)

# Утренний отчёт: еда и активность за вчера (D−1), сон — прошедшая ночь (sleep_data на D).
MORNING_REPORT_MODEL = """
МОДЕЛЬ УТРЕННЕГО ОТЧЁТА (формируется утром после пробуждения, report_day = D):

Источники данных:
- Питание по фото и субъективные заметки — за календарный день D−1 (вчера).
- Активность Garmin (шаги, тренировки, дневной стресс/HRV, Body Battery в течение дня) — за D−1.
- Сон и пробуждение — прошедшая ночь: sleep_data за D (ночь с вечера D−1 до утра D).

В metrics_by_day обычно два дня (D−1 и D) — бери метрики из нужного блока:
- D−1: stats, activities, stress_data, body_battery (дневная динамика), heart_rates и т.д.
- D: sleep_data, morning_training_readiness, утренний Body Battery после пробуждения.
- sleep_data за D−1 — более ранняя ночь; не путай её с «прошедшей ночью» отчёта.

Хронология нарратива:
1) Как прошёл вчера (D−1): нагрузка, питание, заметки.
2) Как прошла прошедшая ночь и пробуждение утром D (сон, recovery).

Причинно-следственные связи:
- Дневная/вечерняя активность и питание D−1 МОГУТ влиять на сон (sleep_data за D) — основная cross-day связь.
- Сон (sleep_data за D) влияет на утреннее состояние в день D, но не на уже прошедшую дневную активность D−1.
- Не связывай дневную активность D−1 как «следствие» сна той же ночи — сон наступил после событий D−1.

Garmin внутри одной даты D (если смотришь только блок D):
- sleep_data за D — до дневных событий календарного D; утренние шаги/тренировки за D минимальны до полного дня.
"""

_RETRY_DELAY_RE = re.compile(r"retry in (\d+(?:\.\d+)?)s", re.I)
_DAILY_QUOTA_MARKERS = (
    "GenerateRequestsPerDayPerProjectPerModel",
    "PerDayPerProjectPerModel-FreeTier",
)


def is_daily_quota_error(error: str | None) -> bool:
    if not error:
        return False
    return any(m in error for m in _DAILY_QUOTA_MARKERS)


def is_retryable_gemini_error(error: str | None) -> bool:
    if not error or is_daily_quota_error(error):
        return False
    return any(x in error for x in ("429", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE"))


def _max_gemini_retries() -> int:
    try:
        return max(1, int(os.environ.get("GEMINI_MAX_RETRIES", "4")))
    except ValueError:
        return 4


def _retry_sleep_seconds(error: str, attempt: int) -> float:
    m = _RETRY_DELAY_RE.search(error)
    if m:
        return float(m.group(1)) + 2.0
    return min(45.0, 8.0 * (attempt + 1))


def _model_chain(preferred: str) -> list[str]:
    chain: list[str] = []
    for name in (
        preferred.strip(),
        os.environ.get("GEMINI_MODEL_FALLBACK", "").strip(),
        "gemini-2.0-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash-lite",
    ):
        if name and name not in chain:
            chain.append(name)
    return chain


def _model_chain_food(preferred: str) -> list[str]:
    """Цепочка моделей для food: без *-lite до исчерпания полноценных (lite часто ломает суммирование БЖУ)."""
    chain: list[str] = []
    for name in (
        preferred.strip(),
        os.environ.get("GEMINI_MODEL_FOOD_FALLBACK", "").strip(),
        os.environ.get("GEMINI_MODEL_FALLBACK", "").strip(),
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ):
        if name and "lite" not in name.lower() and name not in chain:
            chain.append(name)
    if os.environ.get("GEMINI_FOOD_ALLOW_LITE", "").strip().lower() in ("1", "true", "yes"):
        for name in ("gemini-2.5-flash-lite", "gemini-2.0-flash-lite"):
            if name not in chain:
                chain.append(name)
    return chain or _model_chain(preferred)


def _generate_with_retry(
    *,
    contents: Any,
    config: Any,
    model: str,
    context_meta: dict[str, Any] | None = None,
    model_chain: list[str] | None = None,
) -> dict[str, Any]:
    """Вызов Gemini с retry (429/503) и fallback на другие модели при дневной квоте."""
    meta: dict[str, Any] = dict(context_meta or {})
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if genai is None or types is None:
        return {"ok": False, "error": "gemini_sdk_not_installed", "analysis": None, "context": meta}
    if not api_key:
        return {"ok": False, "error": "gemini_not_configured", "analysis": None, "context": meta}

    client = genai.Client(api_key=api_key)
    last_error = ""
    models_tried: list[str] = []
    max_retries = _max_gemini_retries()
    chain = model_chain or _model_chain(model)

    for try_model in chain:
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=try_model,
                    contents=contents,
                    config=config,
                )
                if not response or not getattr(response, "text", None):
                    last_error = "empty_gemini_response"
                    break
                meta_out = {**meta, "model_used": try_model, "models_tried": models_tried + [try_model]}
                return {"ok": True, "analysis": response.text.strip(), "model": try_model, "context": meta_out}
            except Exception as e:
                last_error = str(e)
                if try_model not in models_tried:
                    models_tried.append(try_model)
                if is_daily_quota_error(last_error):
                    logger.warning("Gemini daily quota for model %s, trying fallback", try_model)
                    break
                if is_retryable_gemini_error(last_error) and attempt + 1 < max_retries:
                    delay = _retry_sleep_seconds(last_error, attempt)
                    logger.warning(
                        "Gemini retry %s/%s model=%s sleep=%.0fs: %s",
                        attempt + 1,
                        max_retries,
                        try_model,
                        delay,
                        last_error[:180],
                    )
                    time.sleep(delay)
                    continue
                break

    out: dict[str, Any] = {
        "ok": False,
        "error": last_error,
        "analysis": None,
        "context": {**meta, "models_tried": models_tried},
    }
    if is_daily_quota_error(last_error):
        out["error_kind"] = "gemini_daily_quota_exhausted"
        out["hint"] = (
            "Исчерпан дневной лимит free tier Gemini (20 запросов/модель/день). "
            "Подождите до следующего дня (UTC), включите billing или задайте GEMINI_MODEL_FALLBACK."
        )
    return out


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

""" + MORNING_REPORT_MODEL + """
Формат ответа: на русском языке, структурированно (короткие абзацы или списки), без лишнего вступления. Не придумывай данные — опирайся только на переданные метрики; формулируй причинно-следственные связи как обоснованные гипотезы, а не как факты."""


SYSTEM_PROMPT_DETAILED = """Ты — медицинско-спортивный аналитик данных Garmin с опытом интерпретации носимых датчиков.
Тебе передают сырые данные для УТРЕННЕГО отчёта (report_day = D). Твой ответ сохраняется в архив для
последующего мета-анализа за неделю или месяц другой моделью Gemini.

ГЛАВНАЯ ЗАДАЧА: написать экспертный аналитический отчёт — выводы, интерпретации, связи и нюансы.
НЕ переписывай и НЕ дублируй сырые данные, временные ряды и JSON. Цифры включай только как доказательства
к конкретным выводам (например: «пик стресса 87 около 15:00 совпал с…», а не полный список всех значений).

""" + MORNING_REPORT_MODEL + """
ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (Markdown, все секции заполни; если данных нет — «нет данных»):

## meta
- date: YYYY-MM-DD (report_day D — дата пробуждения и отчёта)
- activity_day: YYYY-MM-DD (D−1 — день активности Garmin)
- overall_day_assessment: 2–4 предложения — сначала как прошёл вчера (D−1), затем прошедшая ночь и пробуждение утром D.
- data_completeness: high/medium/low и что отсутствует для уверенных выводов

## sleep
Экспертная интерпретация sleep_data за report_day D: прошедшая ночь (с вечера D−1 до утра D).
Качество, фазы (deep/REM, ЧСС во сне, SpO2), что мешало восстановлению, связь с утренними метриками на D.
Учитывай влияние активности, стресса и питания вчера (D−1) на этот сон — это допустимая cross-day связь.
Не используй sleep_data за D−1 как «прошедшую ночь» отчёта.

## stress_and_hrv
Стресс и HRV за activity_day D−1 (stress_data, all_day_stress, hrv_data). Пики, вероятные причины,
связь с нагрузкой и восстановлением вчера. Не смешивай с блоком сна за D без явного разделения.

## body_battery_and_recovery
Body Battery и recovery за D−1 (дневная динамика) и утренние показатели на D после пробуждения.

## activity_and_load
Баланс активности за D−1: шаги, этажи, калории, дистанция — достаточно/мало/перебор.
Каждая тренировка (activities) за D−1 — смысл для дня, не таблица полей.
Эффект на восстановление и возможное влияние на сон прошедшей ночи.

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
Если переданы субъективные заметки пользователя — используй их для объяснения аномалий (бег, ванна, поздняя еда,
самочувствие) и свяжи с метриками Garmin; не противоречь заметкам без явного указания на расхождение с данными.

## day_timeline
Хронология отчёта: (1) день D−1 — активность, питание, заметки → (2) ночь D−1→D и пробуждение утром D.
Ключевые моменты с цифрами.

## medical_correlation
Если передан блок «Медицинские данные (Notes/medical_data)» — сопоставь метрики Garmin (RHR, HRV, сон, SpO2,
вес/состав тела, нагрузка) с лабораторными и клиническими показателями из файла: что согласуется, что расходится,
на что обратить внимание в контексте известных отклонений или целей лечения. Если блока нет — «нет данных».
Не ставь диагнозов и не меняй назначения врача — только корреляции и гипотезы для мониторинга.

## correlations_and_insights
Минимум 5 конкретных выводов вида «когда X, то Y, потому что Z» — с нюансами и оговорками.
Учитывай medical_data при наличии. Связь нагрузки/питания D−1 с сном — на sleep_data за D.
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
- Если передан medical_data — опирайся только на показатели из этого блока, не выдумывай анализы.
- Стресс Garmin = HRV-метрика, не психология."""


def _format_user_context_blocks(
    *,
    daily_notes: str | None = None,
    general_notes: str | None = None,
    medical_notes: str | None = None,
) -> str:
    """Блоки субъективного и медицинского контекста для user-сообщения Gemini."""
    parts: list[str] = []
    if general_notes and general_notes.strip():
        parts.append(
            "=== Постоянные пожелания и контекст (Notes/general) ===\n"
            + general_notes.strip()
        )
    if medical_notes and medical_notes.strip():
        parts.append(
            "=== Медицинские данные и анализы (Notes/medical_data) ===\n"
            + medical_notes.strip()
        )
    if daily_notes and daily_notes.strip():
        parts.append(
            "=== Субъективные заметки пользователя за день ===\n"
            + daily_notes.strip()
        )
    return "\n\n".join(parts)


def _call_gemini(
    *,
    metrics: dict[str, Any],
    model: str,
    system_instruction: str,
    user_intro: str,
    temperature: float = 0.4,
    max_output_tokens: int | None = None,
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    daily_notes: str | None = None,
    general_notes: str | None = None,
    medical_notes: str | None = None,
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
    context_blocks = _format_user_context_blocks(
        daily_notes=daily_notes,
        general_notes=general_notes,
        medical_notes=medical_notes,
    )
    meta = {
        **meta,
        "has_daily_notes": bool(daily_notes and daily_notes.strip()),
        "has_general_notes": bool(general_notes and general_notes.strip()),
        "has_medical_notes": bool(medical_notes and medical_notes.strip()),
    }
    if context_blocks:
        user_content = (
            user_intro
            + "\n\n"
            + context_blocks
            + "\n\n=== Данные Garmin Connect ===\n\n"
            + text_context
        )
    else:
        user_content = user_intro + "\n\n" + text_context
    cfg_kwargs: dict[str, Any] = {
        "system_instruction": system_instruction,
        "temperature": temperature,
    }
    if max_output_tokens is not None:
        cfg_kwargs["max_output_tokens"] = max_output_tokens

    return _generate_with_retry(
        contents=user_content,
        config=types.GenerateContentConfig(**cfg_kwargs),
        model=model,
        context_meta=meta,
    )


DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


def model_for_step(step: str, override: str | None = None) -> str:
    """Модель для шага pipeline: archive / food / combined (разные env → разные дневные квоты)."""
    if override:
        return override.strip()
    env_key = {
        "archive": "GEMINI_MODEL",
        "food": "GEMINI_MODEL_FOOD",
        "combined": "GEMINI_MODEL_COMBINED",
    }.get(step, "GEMINI_MODEL")
    return (os.environ.get(env_key, "") or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL


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
    *,
    daily_notes: str | None = None,
    general_notes: str | None = None,
    medical_notes: str | None = None,
) -> dict[str, Any]:
    """
    Экспертный архивный анализ за день для GCS archive/.
    Только выводы и интерпретации — без дублирования сырых метрик в ответе.
    """
    model = model_for_step("archive", model)
    return _call_gemini(
        metrics=metrics,
        model=model,
        system_instruction=SYSTEM_PROMPT_DETAILED,
        user_intro=(
            "Ниже сырые данные Garmin Connect для утреннего отчёта (report_day = дата пробуждения). "
            "Активность, стресс и нагрузка — за вчера (D−1); sleep_data на report_day D — прошедшая ночь. "
            "Если есть субъективные заметки — они за вчера (D−1), учитывай для пульса, стресса и Body Battery. "
            "Если есть medical_data — сопоставь Garmin с лабораторными и клиническими показателями. "
            "Сформируй экспертный отчёт по обязательной структуре. "
            "Не переписывай сырые данные. "
            "Текст позже объединят для анализа трендов за неделю/месяц."
        ),
        temperature=0.25,
        daily_notes=daily_notes,
        general_notes=general_notes,
        medical_notes=medical_notes,
        max_output_tokens=8192,
    )


SYSTEM_PROMPT_FOOD = """Ты — диетолог-нутрициолог с экспертизой в оценке питания по фотографиям еды
и в evidence-based подходах (клетчатка, микробиом кишечника, разнообразие растений, ферментированные продукты).
Тебе передают фото приёмов пищи за ВЧЕРАШНИЙ календарный день (food_day = D−1) в рамках утреннего отчёта
за дату пробуждения report_day = D. Твой отчёт сохраняется в архив для отслеживания дневных норм
и последующего мета-анализа за неделю/месяц.

ГЛАВНАЯ ЗАДАЧА: экспертный анализ питания за D−1 — что съедено, оценка порций, макро- и микронутриенты,
сравнение с дневными нормами, влияние на микробиом, выводы и рекомендации по лучшим практикам.
Не перечисляй сырые данные с фото — только интерпретации и расчёты. Явно указывай степень уверенности
(высокая/средняя/низкая) для каждой оценки.
Если передан блок «Постоянные пожелания (Notes/general)» — обязательно учитывай его критерии
(например интервальное голодание, типичные напитки вне фото).
Если передан блок «Медицинские данные (Notes/medical_data)» — сопоставляй питание дня с известными
лабораторными показателями (липиды, глюкоза/HbA1c, ферритин, витамины, давление и т.д.): дефициты,
избытки, риски; не ставь диагнозов.

""" + MORNING_REPORT_MODEL + """
Нормы: используй рекомендуемые суточные нормы для взрослого (RDA/DRI, EU/NRV) — ~2000 kcal baseline,
если профиль пользователя не указан. Укажи, какие нормы применяешь.

Ориентиры лучших практик (применяй как рамку оценки, не как жёсткий протокол):
- клетчатка: ~25–38 г/день; разнообразие растительных источников (цель ≥30 разных растений/неделю — оцени вклад дня)
- пробиотики: ферментированные продукты (йогурт, кефир, квашеная капуста, miso, kimchi и т.д.)
- пребиотики: овощи, бобовые, цельные злаки, лук/чеснок, бананы, зелёные
- полифенолы и растительное разнообразие для поддержки микробиома
- ограничение ultra-processed foods, избытка добавленного сахара и насыщенных жиров
- баланс омега-3/омега-6, достаточный белок, умеренный glycemic load
- регулярность и распределение приёмов пищи

ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (Markdown, все секции; если данных нет — «нет данных»):

## meta
- date: YYYY-MM-DD (food_day = D−1 — день питания по фото)
- report_day: YYYY-MM-DD (дата утреннего отчёта / пробуждения)
- photos_analyzed: число фото и кратко что на каждом (завтрак/обед/перекус и т.д.)
- overall_nutrition_assessment: 2–4 предложения — баланс дня
- confidence: high/medium/low и почему

## meals_breakdown
По каждому приёму пищи (по фото или логически сгруппированно):
- идентификация блюд и ингредиентов
- оценка порций (граммы/объём, метод оценки)
- ключевые сахара, насыщенные/ненасыщенные жиры — если можно оценить
- **БЖУ строка (обязательна в конце каждого приёма, числа для суммирования):**
  `БЖУ: kcal N | белок N г | жир N г | углев N г | клетчатка N г`

## arithmetic_check
**Заполни ДО daily_totals.** Таблица суммирования всех строк БЖУ из meals_breakdown:

| Приём пищи | kcal | белок г | жир г | углев г | клетчатка г |
|------------|------|---------|-------|---------|-------------|
| ...        | ...  | ...     | ...   | ...     | ...         |
| **СУММА**  | X    | Y       | Z     | W       | F           |

Под таблицей — явное сложение: «150 + 750 + … = X kcal» (хотя бы для калорий и белка).

## daily_totals
Суммарно за день — **только цифры из строки СУММА в arithmetic_check** (не округляй заново, не «примерно»):
- калории (kcal) = X из СУММЫ; % от суточной нормы
- белки, жиры, углеводы, клетчатка (г) = из СУММЫ; % от нормы
- омега-3/омега-6 — если оценимо
- если daily_totals не совпадает с СУММОЙ — исправь daily_totals, не meals_breakdown

## vitamins
Оценка витаминов (A, C, D, E, K, B1, B2, B3, B5, B6, B9/folate, B12, биотин, холин):
для каждого — оценка потребления (% от суточной нормы или «недостаточно данных»), significance.

## minerals
Оценка минералов (Ca, Fe, Mg, P, K, Na, Zn, Cu, Mn, Se, I, Cr, Mo):
для каждого — % от нормы или «недостаточно данных», significance.

## phytonutrients_and_antioxidants
Полифенолы, carotenoids, флавоноиды, антоцианы, sulforaphane, lycopene и др. —
что вероятно получено с едой дня, чего не хватает, практический смысл.

## microbiome_impact
Экспертная оценка влияния рациона дня на микробиом кишечника:
- клетчатка: достаточно/мало; soluble vs insoluble — если можно оценить
- разнообразие растений: сколько уникальных растительных групп/ингредиентов за день (овощи, фрукты, бобовые, орехи, злаки, специи)
- пробиотики: были ли ферментированные продукты; чего не хватило
- пребиотики: inulin-обогащённые, резистентный крахмал, Allium и т.д.
- полифенолы и их вероятный эффект на gut microbiota
- негативные факторы: ultra-processed, избыток сахара/Na, мало клетчатки, однообразие
- общий вердикт: supportive / neutral / potentially harmful для микробиома (с evidence и confidence)
- что изменить в первую очередь для улучшения gut health

## deficiencies_and_excesses
Дефициты и избытки относительно норм с severity (low/medium/high) и evidence.
Если есть medical_data — учитывай персональные целевые диапазоны и известные отклонения из анализов.

## medical_correlation
Если передан блок medical_data — сопоставь питание дня с лабораторными показателями (липиды, глюкоза, ферритин,
витамины, давление и т.д.): что поддерживает цели, что усугубляет риски. Если блока нет — «нет данных».
Не ставь диагнозов.

## meal_quality_insights
Качество рациона: индекс обработанности, разнообразие, баланс БЖУ, glycemic load если уместно,
timing приёмов пищи (если видно по контексту фото/имени файла).

## correlations_with_health
Как питание D−1 может влиять на сон прошедшей ночи (sleep_data на report_day D), recovery и метрики Garmin.
При наличии medical_data — корреляции с клиническими показателями. Гипотезы, не диагнозы.

## evidence_based_recommendations
5–8 конкретных рекомендаций на завтра и ближайшую неделю, основанных на лучших практиках нутрициологии:
- что добавить (конкретные продукты/группы, не абстракции)
- что уменьшить или заменить
- как закрыть выявленные дефициты (клетчатка, пробиотики, микронутриенты)
- приоритет: high/medium/low для каждой рекомендации
- кратко: на какой принцип/практику опирается рекомендация (например: «разнообразие растений», «≥25 г клетчатки»)

## data_gaps
Что невозможно оценить по фото (скрытые ингредиенты, масло, соусы, точный вес, напитки без фото).

## coverage_warning
Только если анализ неполный (есть пропущенные фото в манифесте): что не учтено и как это ограничивает выводы.
Если все фото обработаны — «полное покрытие».

## facts_for_aggregation
Компактные факты для merge — одна строка:
- FACT | category=nutrition | nutrient=... | amount=... | pct_rda=... | severity=... | note=...
- FACT | category=microbiome | aspect=... | verdict=... | evidence=... | note=...

Правила:
- Язык: русский.
- Оценки по фото — приблизительные; не выдавай их за лабораторный анализ.
- Не придумывай блюда, которых не видно на фото.
- Учитывай скрытые калории (масло, соусы) как диапазон, если не видны.
- Рекомендации по микробиому — evidence-informed, без псевдонаучных claims.

ОЦЕНКА ПОРЦИЙ — НЕ ЗАВЫШАЙ БЕЗ ОСНОВАНИЙ:
- Граммы и kcal — из того, что видно на фото, а не из «типичной ресторанной» или максимальной порции категории.
- Щедрые допущения (двойная начинка, ложка майонеза, большой кусок пирога, лишнее масло) — только если
  это явно видно по слоям, размеру относительно посуды или упаковке.
- Скрытое масло/соус, которого не видно — разумная минимальная оценка или диапазон, не верхняя граница.
- При сомнении между лёгкой и тяжёлой версией — средняя или более скромная оценка; поясни в meals_breakdown.

ЧТЕНИЕ ЭТИКЕТОК И УПАКОВКИ (критично — не галлюцинируй):
- Если на фото видна таблица пищевой ценности — прочитай её буквально: бренд, название продукта, значения «на 100 г/100 мл»
  и «на порцию», если указаны. Не подставляй типичные значения «похожего» продукта.
- Натуральный кефир/йогурт без сахара ≠ сладкий питьевой йогурт — ориентируйся на название и цифры на этикетке.
- Пересчёт: (граммы/мл порции ÷ 100) × значение на 100 г. Покажи формулу в meals_breakdown для упакованных продуктов.
- Если этикетка частично нечитаема — дай диапазон и пометь confidence: low; не выдавай точные цифры за факт.
- Объёмы «на глаз» (сливки в кофе, масло): используй визуальные подсказки (цвет, слои, размер посуды);
  при сомнении — диапазон (например 25–35 мл), не завышай до верхней границы без оснований.
- **Жиры в жидкостях:** жир (г) = объём_мл × (жирность_% / 100). Сливки Fresh Cream / cooking cream ~30–38%:
  30 мл при 35% ≈ 10.5 г жира, не 3 г. Укажи % с упаковки или обоснуй оценку.

АРИФМЕТИКА И СОГЛАСОВАННОСТЬ (критично — частая ошибка моделей):
- Сначала meals_breakdown (каждый приём с БЖУ строкой) → arithmetic_check (таблица + СУММА) → daily_totals (= СУММА).
- **Запрещено** писать в daily_totals числа, которые не равны СУММЕ (допуск ±3% только на клетчатку).
- deficiencies_and_excesses, microbiome_impact, overall_nutrition_assessment — **только из daily_totals после arithmetic_check**,
  не из «ощущений» и не из устаревших промежуточных оценок.
- Если клетчатка в СУММЕ ≥25 г — не пиши «критический дефicit клетчатки».
- Не дублируй один приём дважды в таблице; не пропускай приёмы из meals_breakdown.

ПОЛНОТА АНАЛИЗА (критично):
- Обработай КАЖДОЕ переданное фото по порядку, прежде чем считать daily_totals.
- В meta укажи photos_analyzed = число обработанных фото и перечисли имя файла + что на каждом.
- Если в манифесте указаны пропущенные файлы или photos_found > photos_analyzed — секция ## coverage_warning обязательна:
  перечисли что не проанализировано; пометь overall confidence: low; НЕ делай выводов о критическом дефиците
  (клетчатка, белок, калории, микробиом) по неполным данным — только по проанализированным фото.
- daily_totals и вердикты по дефицитам — только если analysis_complete или явно оговорено «по N из M фото»."""


def _food_photo_manifest_text(fetch_meta: dict[str, Any] | None, photos: list[dict[str, Any]]) -> str:
    """Текст манифеста: сколько фото в папке, что передано, что пропущено."""
    if not fetch_meta:
        names = [p.get("name") or f"photo_{i}" for i, p in enumerate(photos, 1)]
        return (
            f"Передано фото: {len(photos)}.\n"
            f"Список файлов (обработай каждый): {', '.join(names)}."
        )

    images_found = int(fetch_meta.get("images_found") or fetch_meta.get("photo_count") or len(photos))
    skipped = fetch_meta.get("skipped") or []
    analyzed_names = [p.get("name") or "?" for p in photos]
    lines = [
        f"В папке Drive найдено изображений: {images_found}",
        f"Передано в анализ: {len(photos)}",
        f"analysis_complete: {bool(fetch_meta.get('analysis_complete', not skipped and images_found == len(photos)))}",
        "Файлы в этом запросе (обработай каждый по порядку):",
        *[f"  {i}. {name}" for i, name in enumerate(analyzed_names, 1)],
    ]
    if skipped:
        lines.append("НЕ переданы в модель (итоги дня будут неполными — см. coverage_warning):")
        for s in skipped:
            lines.append(f"  - {s.get('name', '?')}: {s.get('reason', 'skipped')}")
    return "\n".join(lines)


def analyze_food_photos(
    photos: list[dict[str, Any]],
    *,
    day_label: str,
    report_day: str | None = None,
    model: str | None = None,
    profile_hint: str | None = None,
    general_notes: str | None = None,
    medical_notes: str | None = None,
    fetch_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Анализ фото еды за день через Gemini Vision (multimodal).

    В отличие от Gemini в веб-чате с ссылкой на Google Drive (OCR этикеток, лимит ~5 файлов,
    без «зрения» тарелок), MyBody скачивает байты через Drive API и передаёт Part.from_bytes —
    полноценный visual analysis каждого фото.
    """
    if genai is None or types is None:
        return {"ok": False, "error": "gemini_sdk_not_installed", "analysis": None}
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "gemini_not_configured", "analysis": None}
    if not photos:
        return {"ok": False, "error": "no_photos", "analysis": None}

    model = model_for_step("food", model)
    manifest = _food_photo_manifest_text(fetch_meta, photos)
    report_day_label = report_day or day_label
    intro = (
        f"Утренний отчёт: report_day (пробуждение) = {report_day_label}.\n"
        f"День питания по фото (food_day) = {day_label}.\n\n"
        f"=== Манифест фото ===\n{manifest}\n\n"
        f"Ниже {len(photos)} изображений. Сначала разбери КАЖДОЕ фото (meals_breakdown с БЖУ строкой), "
        "затем arithmetic_check (таблица + СУММА), затем daily_totals = СУММА без расхождений. "
        "Не завышай порции без визуальных оснований. "
        "На упакованных продуктах читай этикетку с фото буквально, не угадывай по категории. "
        "На фото тарелок и блюд — визуально оценивай состав, объём порций и калории (это полноценные изображения, не OCR). "
        "Сформируй отчёт по обязательной структуре из инструкции."
    )
    context_blocks = _format_user_context_blocks(
        general_notes=general_notes,
        medical_notes=medical_notes,
    )
    if context_blocks:
        intro += f"\n\n{context_blocks}"
    if profile_hint:
        intro += f"\n\nПрофиль пользователя (если релевантно для норм): {profile_hint}"

    parts: list[Any] = [intro]
    sent = 0
    gemini_resized = 0
    for i, photo in enumerate(photos, start=1):
        name = photo.get("name") or f"photo_{i}"
        mime = photo.get("mime_type") or "image/jpeg"
        data = photo.get("data")
        if not data:
            continue
        data, mime, gemini_adj = drive_client.prepare_photo_for_gemini(data, mime)
        if gemini_adj != "none":
            gemini_resized += 1
        sent += 1
        parts.append(f"\n--- Фото {i} из {len(photos)}: {name} ---")
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))

    images_found = int((fetch_meta or {}).get("images_found") or sent)
    meta = {
        "photo_count": sent,
        "photos_found_in_folder": images_found,
        "photos_skipped_count": int((fetch_meta or {}).get("photos_skipped_count") or 0),
        "analysis_complete": bool((fetch_meta or {}).get("analysis_complete", sent == images_found)),
        "gemini_inline_resized_count": gemini_resized,
        "delivery_mode": "inline_bytes_vision",
        "day": day_label,
    }
    return _generate_with_retry(
        contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT_FOOD,
            temperature=0.15,
            max_output_tokens=16384,
        ),
        model=model,
        context_meta=meta,
        model_chain=_model_chain_food(model),
    )


SYSTEM_PROMPT_COMBINED = """Ты — персональный health-coach: спортивная медицина, сон, восстановление и нутрициология.
Тебе передают три источника для УТРЕННЕГО отчёта (report_day = D, дата пробуждения):
1) сырые данные Garmin Connect (активность за D−1 + сон прошедшей ночи на D);
2) архивный экспертный анализ питания за D−1 (по фото вчерашних приёмов пищи);
3) субъективные заметки пользователя за D−1 (контекст вчерашнего дня).

Задача: СОГЛАСОВАТЬ Garmin, питание и заметки с правильной хронологией, найти связи, противоречия и приоритеты.
Если передан general — учитывай его во всех выводах. Если передан medical_data — коррелируй Garmin и питание
с лабораторными и клиническими показателями; не ставь диагнозов и не меняй назначения врача.
Этот отчёт пойдёт пользователю в email и HTML — пиши ясно, структурированно, без сырых JSON и без
переписывания входных данных. Цифры — только как доказательства к выводам.

""" + MORNING_REPORT_MODEL + """
ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (Markdown):

## executive_summary
3–5 предложений: (1) как прошёл вчера (D−1) — нагрузка и питание; (2) прошедшая ночь и пробуждение утром D;
(3) главный вывод и фокус на сегодня. Связывай питание/нагрузку D−1 с качеством сна (sleep_data на D) где уместно.

## user_notes_context
Кратко: что пользователь сам отметил за вчера (D−1) и как это может объяснить метрики Garmin и сон прошедшей ночи.

## garmin_key_points
Главное из Garmin: сон (sleep_data на D), вчерашний стресс/HRV и нагрузка (D−1), Body Battery — списком,
каждый пункт с тегом [good|warn|neutral|bad] и **меткой:** (см. правила оценки ниже).

## nutrition_key_points
Главное из питания за D−1 — списком с тегами и **метками:** как выше.
Опирайся на **daily_totals / arithmetic_check** из архива питания, не пересчитывай заново и не занижай калории/белок,
если в meals_breakdown сумма выше.

## medical_key_points
Если передан medical_data — главные корреляции лабораторных/клинических показателей с Garmin и питанием,
списком с тегами и **метками:**. Если блока нет — «нет медицинских данных для корреляции».

## cross_domain_insights
Минимум 4 связи «Garmin ↔ питание ↔ заметки ↔ medical_data (если есть)» с учётом утренней модели:
- нагрузка/питание/заметки D−1 ↔ сон прошедшей ночи (sleep_data на D);
- нагрузка/стресс D−1 ↔ Body Battery и recovery;
- недобор белка/клетчатки D−1 ↔ восстановление и сон;
- перегруз D−1 ↔ калории/углеводы и сон;
- кофеин/алкоголь (если в питании D−1) ↔ сон/HRV на D;
- лабораторные показатели (medical_data) ↔ питание D−1 и метрики Garmin (RHR, HRV, сон, вес).
Различай факты и гипотезы.

## unified_recommendations
5–7 конкретных рекомендаций на завтра. Каждая строка:
- [priority: high|medium|low] **[warn] Короткая метка:** что делать.

## watch_out
2–4 риска (severity + evidence). Каждая строка с меткой **[bad]** или **[warn]**.

## tomorrow_focus
Одно предложение — главный фокус; начни с **[good]** или **[warn]** и жирной метки.

ОЦЕНКА УРОВНЯ ДЛЯ ПОДСВЕТКИ В EMAIL (обязательно на каждом пункте списка и абзаце с меткой):
Перед жирной меткой укажи тег в квадратных скобках — он попадёт только в заголовок пункта, не весь текст:
- **[good]** — явно хорошо, в норме, цель достигнута, excellent/good
- **[warn]** — удовлетворительно, FAIR, ниже оптимального, зона внимания, mixed, medium priority
- **[neutral]** — без явной оценки, факт без вывода
- **[bad]** — плохо, high severity, критичный риск, сильный дефицит

Формат пункта (строго):
- **[warn] Сон:** текст...
- **[good] Нагрузка:** текст...

Не ставь [good] на «удовлетворительно», FAIR, «ниже оптимального» — для них только [warn].

Правила:
- Язык: русский.
- Если анализа питания нет — явно укажи и опирайся на Garmin и заметки.
- Если заметок нет — явно укажи и опирайся на Garmin и питание.
- Стресс Garmin = HRV-метрика, не психология.
- Не дублируй длинные архивные тексты — синтезируй.
- Не ставь медицинских диагнозов."""


def analyze_daily_combined(
    metrics: dict[str, Any],
    *,
    food_analysis: str | None,
    daily_notes: str | None = None,
    day_label: str,
    food_day: str | None = None,
    general_notes: str | None = None,
    medical_notes: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Итоговый согласованный отчёт: сырые Garmin + архивный анализ питания → рекомендации для email/outcomes.
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

    model = model_for_step("combined", model)
    text_context, meta = build_prompt_context(metrics)
    activity_day = str(metrics.get("activity_day") or food_day or day_label)
    nutrition_day = str(food_day or metrics.get("food_day") or activity_day)
    meta = {
        **meta,
        "day": day_label,
        "report_day": day_label,
        "activity_day": activity_day,
        "food_day": nutrition_day,
        "has_food_analysis": bool(food_analysis and food_analysis.strip()),
        "has_daily_notes": bool(daily_notes and daily_notes.strip()),
        "has_general_notes": bool(general_notes and general_notes.strip()),
        "has_medical_notes": bool(medical_notes and medical_notes.strip()),
    }

    food_block = (
        food_analysis.strip()
        if food_analysis and food_analysis.strip()
        else f"нет архивного анализа питания за {nutrition_day}"
    )
    notes_block = (
        daily_notes.strip()
        if daily_notes and daily_notes.strip()
        else f"нет субъективных заметок пользователя за {activity_day}"
    )
    context_blocks = _format_user_context_blocks(
        general_notes=general_notes,
        medical_notes=medical_notes,
        daily_notes=notes_block,
    )
    user_content = (
        f"Утренний отчёт: report_day (пробуждение) = {day_label}.\n"
        f"Активность Garmin и заметки — за {activity_day}.\n"
        f"Питание — за {nutrition_day}.\n"
        f"Сон — прошедшая ночь (sleep_data на {day_label}).\n\n"
        "Согласуй источники с правильной хронологией и сформируй отчёт по структуре из инструкции.\n\n"
        f"{context_blocks}\n\n"
        "=== Архивный экспертный анализ питания (вчера) ===\n"
        f"{food_block}\n\n"
        "=== Сырые данные Garmin Connect ===\n"
        f"{text_context}"
    )

    return _generate_with_retry(
        contents=user_content,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT_COMBINED,
            temperature=0.35,
            max_output_tokens=8192,
        ),
        model=model,
        context_meta=meta,
    )
