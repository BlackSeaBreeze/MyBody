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

# Garmin привязывает sleep_data к дате пробуждения — модели часто путают порядок «сон ↔ активность дня».
GARMIN_DAY_CHRONOLOGY = """
ХРОНОЛОГИЯ КАЛЕНДАРНОГО ДНЯ В GARMIN (критично — не путай причину и следствие):
- Все данные за дату D (YYYY-MM-DD) описывают календарный день D.
- sleep_data за D — сон, ЗАКОНЧИВШИЙСЯ утром D (ночь с D−1 на D). Это ПРОШЛАЯ ночь, ДО дневных событий D.
- Утренние показатели (Body Battery при пробуждении, morning readiness, утренний RHR) — итог этого сна, ещё ДО активности D.
- Шаги, тренировки, стресс днём, калории, питание и заметки пользователя за D — ПОСЛЕ пробуждения, ПОСЛЕ sleep_data за D.
- Сон «сегодня вечером» после активности D в данных за D отсутствует — он попадёт в sleep_data за D+1.
- ЗАПРЕЩЕНО: «несмотря на активный день, сон был плохим» / «активность повлияла на сон» для sleep_data за D —
  этот сон был РАНЬШЕ активности. Велопрогулка/нагрузка D не объясняет сон, закончившийся утром D.
- Питание и нагрузка D могут влиять на сон ночи D→D+1 (будущие данные), но НЕ на sleep_data за D.
- В summary и executive_summary: сначала прошедшая ночь (сон → пробуждение), затем как прошёл день после пробуждения.
- Плохой сон утром D + высокая активность днём D: ищи причины сна во вечере/ночи D−1, режиме, стрессе вчера — не в дневной активности D.
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

""" + GARMIN_DAY_CHRONOLOGY + """
Формат ответа: на русском языке, структурированно (короткие абзацы или списки), без лишнего вступления. Не придумывай данные — опирайся только на переданные метрики; формулируй причинно-следственные связи как обоснованные гипотезы, а не как факты."""


SYSTEM_PROMPT_DETAILED = """Ты — медицинско-спортивный аналитик данных Garmin с опытом интерпретации носимых датчиков.
Тебе передают сырые данные за ОДИН день. Твой ответ сохраняется в архив для последующего мета-анализа
за неделю или месяц другой моделью Gemini.

ГЛАВНАЯ ЗАДАЧА: написать экспертный аналитический отчёт — выводы, интерпретации, связи и нюансы.
НЕ переписывай и НЕ дублируй сырые данные, временные ряды и JSON. Цифры включай только как доказательства
к конкретным выводам (например: «пик стресса 87 около 15:00 совпал с…», а не полный список всех значений).

""" + GARMIN_DAY_CHRONOLOGY + """
ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (Markdown, все секции заполни; если данных нет — «нет данных»):

## meta
- date: YYYY-MM-DD
- overall_day_assessment: 2–4 предложения — сначала итог прошедшей ночи (сон → пробуждение), затем день после пробуждения (нагрузка, восстановление, риски). Не связывай дневную активность как причину sleep_data за эту дату.
- data_completeness: high/medium/low и что отсутствует для уверенных выводов

## sleep
Экспертная интерпретация sleep_data за дату D: это ночь с D−1 на D (пробуждение утром D), ДО дневной активности.
Качество, фазы (deep/REM, ЧСС во сне, SpO2), что мешало восстановлению, связь с утренними метриками.
Не приписывай этому сну события дня D (тренировки, питание), произошедшие после пробуждения.

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
Если переданы субъективные заметки пользователя — используй их для объяснения аномалий (бег, ванна, поздняя еда,
самочувствие) и свяжи с метриками Garmin; не противоречь заметкам без явного указания на расхождение с данными.

## day_timeline
Хронология строго по Garmin: (1) сон ночи D−1→D, пробуждение утром D → (2) утро и дневная активность D → (3) вечер D.
Не ставь сон после тренировок/питания дня D. Ключевые моменты с цифрами.

## correlations_and_insights
Минимум 5 конкретных выводов вида «когда X, то Y, потому что Z» — с нюансами и оговорками.
Не делай выводов «активность D ухудшила сон D» — sleep_data за D был до активности. Связь нагрузки/питания D с сном формулируй как влияние на предстоящую ночь D→D+1 (гипотеза).
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
    daily_notes: str | None = None,
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
    if daily_notes and daily_notes.strip():
        meta = {**meta, "has_daily_notes": True}
        user_content = (
            user_intro
            + "\n\n=== Субъективные заметки пользователя за день ===\n"
            + daily_notes.strip()
            + "\n\n=== Данные Garmin Connect ===\n\n"
            + text_context
        )
    else:
        meta = {**meta, "has_daily_notes": False}
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
            "Ниже сырые данные Garmin Connect за один календарный день. "
            "Помни: sleep_data за эту дату — прошедшая ночь (до пробуждения), активность и заметки — после пробуждения. "
            "Если есть субъективные заметки — учитывай их для пульса, стресса и Body Battery в течение дня; "
            "не объясняй ими sleep_data за эту же дату как последствие дневных событий. "
            "Сформируй экспертный отчёт по обязательной структуре. "
            "Не переписывай сырые данные. "
            "Текст позже объединят для анализа трендов за неделю/месяц."
        ),
        temperature=0.25,
        daily_notes=daily_notes,
        max_output_tokens=8192,
    )


SYSTEM_PROMPT_FOOD = """Ты — диетолог-нутрициолог с экспертизой в оценке питания по фотографиям еды
и в evidence-based подходах (клетчатка, микробиом кишечника, разнообразие растений, ферментированные продукты).
Тебе передают фото приёмов пищи за ОДИН календарный день. Твой отчёт сохраняется в архив для
отслеживания дневных норм и последующего мета-анализа за неделю/месяц.

ГЛАВНАЯ ЗАДАЧА: экспертный анализ питания — что съедено, оценка порций, макро- и микронутриенты,
сравнение с дневными нормами, влияние на микробиом, выводы и рекомендации по лучшим практикам.
Не перечисляй сырые данные с фото — только интерпретации и расчёты. Явно указывай степень уверенности
(высокая/средняя/низкая) для каждой оценки.

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
- date: YYYY-MM-DD
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

## meal_quality_insights
Качество рациона: индекс обработанности, разнообразие, баланс БЖУ, glycemic load если уместно,
timing приёмов пищи (если видно по контексту фото/имени файла).

## correlations_with_health
Как питание дня D может влиять на предстоящий сон (ночь D→D+1), стресс и восстановление в течение D и на следующий день —
не на sleep_data за D (он уже был утром, до приёмов пищи D). Гипотезы, не медицинские диагнозы.

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
- Если несколько фото одного приёма — объединяй, не дублируй калории.
- Учитывай скрытые калории (масло, соусы) как диапазон, если не видны.
- Рекомендации по микробиому — evidence-informed, без псевдонаучных claims.

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
    model: str | None = None,
    profile_hint: str | None = None,
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
    intro = (
        f"Дата: {day_label}.\n\n"
        f"=== Манифест фото ===\n{manifest}\n\n"
        f"Ниже {len(photos)} изображений. Сначала разбери КАЖДОЕ фото (meals_breakdown с БЖУ строкой), "
        "затем arithmetic_check (таблица + СУММА), затем daily_totals = СУММА без расхождений. "
        "На упакованных продуктах читай этикетку с фото буквально, не угадывай по категории. "
        "На фото тарелок и блюд — визуально оценивай состав, объём порций и калории (это полноценные изображения, не OCR). "
        "Сформируй отчёт по обязательной структуре из инструкции."
    )
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
Тебе передают три источника за ОДИН календарный день:
1) сырые данные Garmin Connect;
2) архивный экспертный анализ питания (уже посчитан по фото еды);
3) субъективные заметки пользователя (контекст дня: активность, ванна, самочувствие и т.д.).

Задача: СОГЛАСОВАТЬ Garmin, питание и заметки, найти связи, противоречия и приоритеты.
Этот отчёт пойдёт пользователю в email и HTML — пиши ясно, структурированно, без сырых JSON и без
переписывания входных данных. Цифры — только как доказательства к выводам.

""" + GARMIN_DAY_CHRONOLOGY + """
ОБЯЗАТЕЛЬНАЯ СТРУКТУРА (Markdown):

## executive_summary
3–5 предложений: (1) прошедшая ночь и пробуждение, (2) как прошёл день после пробуждения (нагрузка, питание, заметки).
Не пиши, что дневная активность «несмотря на это» ухудшила сон за эту дату — сон в Garmin за D был до активности D.

## user_notes_context
Кратко: что пользователь сам отметил за день и как это может объяснить метрики Garmin (пульс, стресс, сон, Body Battery).

## garmin_key_points
Главное из Garmin: сон, стресс/HRV, Body Battery, нагрузка — списком, каждый пункт с тегом [good|warn|neutral|bad] и **меткой:** (см. правила оценки ниже).

## nutrition_key_points
Главное из питания — списком с тегами и **метками:** как выше.
Опирайся на **daily_totals / arithmetic_check** из архива питания, не пересчитывай заново и не занижай калории/белок,
если в meals_breakdown сумма выше.

## cross_domain_insights
Минимум 4 связи «Garmin ↔ питание ↔ заметки», с соблюдением хронологии Garmin:
- нагрузка/питание/заметки дня D ↔ метрики днём D и Body Battery после пробуждения (не ↔ sleep_data за D как следствие);
- поздний/тяжёлый ужин D ↔ возможный сон ночи D→D+1 (гипотеза на будущее), не ↔ утренний сон D;
- недобор белка/клетчатки ↔ восстановление;
- перегруз ↔ калории/углеводы;
- кофеин/алкоголь (если упомянуты в питании) ↔ сон/HRV.
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
    meta = {
        **meta,
        "day": day_label,
        "has_food_analysis": bool(food_analysis and food_analysis.strip()),
        "has_daily_notes": bool(daily_notes and daily_notes.strip()),
    }

    food_block = (
        food_analysis.strip()
        if food_analysis and food_analysis.strip()
        else "нет архивного анализа питания за этот день"
    )
    notes_block = (
        daily_notes.strip()
        if daily_notes and daily_notes.strip()
        else "нет субъективных заметок пользователя за этот день"
    )
    user_content = (
        f"Дата: {day_label}.\n\n"
        "Ниже Garmin, питание и заметки за этот календарный день. "
        "sleep_data за эту дату = прошедшая ночь (до пробуждения); активность, питание и заметки = после пробуждения. "
        "Согласуй источники с правильной хронологией и сформируй отчёт по структуре из инструкции.\n\n"
        "=== Субъективные заметки пользователя ===\n"
        f"{notes_block}\n\n"
        "=== Архивный экспертный анализ питания ===\n"
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
