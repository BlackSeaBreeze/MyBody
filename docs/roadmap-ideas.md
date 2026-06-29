# MyBody — идеи и roadmap

Черновик для обсуждения по ходу разработки. Не обязательства — приоритеты могут меняться.

**Последнее обновление:** 2026-06-13

---

## Высокий приоритет (надёжность)

### 1. Уведомление при сбое pipeline

Сейчас при 502 / квоте Gemini письма с отчётом нет — легко не заметить.

**Идея:** короткое письмо «MyBody: daily failed» с `error`, `gemini_daily_quota`, ссылкой на логи Cloud Run.

**Статус:** сделано — `_maybe_send_failure_alert` в daily/weekly при `send=true` и отсутствии обычного письма; env `PIPELINE_FAILURE_ALERTS` (по умолчанию включено), `CLOUD_RUN_LOGS_URL` (опц.).

---

### 2. Алерты в GCP (без дублирования с п.1)

**Не дублировать** email из приложения:

- при успешном failure-alert endpoint возвращает **HTTP 200** → Scheduler не считает job проваленным;
- **не** алертить на все 5xx Cloud Run для `/internal/*`.

**GCP ловит дыры:**

- Scheduler `attempt_failed` (timeout, 502 когда письмо не ушло);
- лог `MYBODY_SAFETY_NET` — pipeline упал, failure-alert не отправлен (SMTP, crash).

**Статус:** сделано — `scripts/setup_gcp_alerts.ps1`, `docs/gcp-monitoring.md`, лог `MYBODY_SAFETY_NET` в `main.py`.

---

## Сон (главный фокус)

### 3. Rollup сна в коде до Gemini

Для weekly (и позже monthly) считать в Python из `key_metrics` / `sleep_metrics`:

- среднее / min / max `total_sleep`;
- тренд (↑↓→);
- «плохие ночи» (<6 ч, score <60).

**Зачем:** меньше токенов, стабильнее отчёт, не зависит от full vs FACT-digest.

**Статус:** не начато

---

### 4. Мини-таблица сна в weekly-письме

7 строк: дата | сон | score | restless — из архивов, без Gemini.

**Зачем:** цифры всегда на виду, даже если текст анализа «водянистый».

**Статус:** не начато

---

### 5. Единый `sleep_metrics` в архиве

Сейчас в GCS в основном текст Gemini.

**Идея:** детерминированно дописывать в frontmatter JSON `sleep_metrics` из `sleep_report` при сохранении daily.

**Зачем:** weekly FACT-digest точнее, не зависит от оформления секции `## sleep_metrics` моделью.

**Статус:** не начато

---

### 6. Формат длительности сна

> **Сделано:** `format_sleep_duration` — >60 мин → `ч:мм` (7:05), иначе «N мин»; email, Garmin view, промпты Gemini.

---

## Weekly / мета-анализ

### 7. Сохранять weekly MD в GCS

Сейчас только `outcomes/weekly-….html`.

**Идея:** `archive/weekly-vb-….md` для month-over-month и чата.

**Статус:** не начато

---

### 8. `count_tokens` перед weekly

Вместо оценки `len/1.35` — реальный `count_tokens` от Gemini для решения full vs FACT-digest.

**Зачем:** точнее укладываться в free tier (задел: `scripts/measure_tokens.py`).

**Статус:** не начато

---

### 9. Monthly на той же базе

Тот же endpoint с `days=30`, по умолчанию FACT-digest + rollup.

**Зачем:** monthly почти бесплатен по архитектуре после weekly.

**Статус:** не начато

---

### 10. Weekly report endpoint

> **Сделано:** `POST /internal/weekly-report` — полные архивы из GCS, fallback на FACT-digest, disclaimer в письме, cron +1 ч после daily.

---

## Питание

### 11. Жёстче обрабатывать пропущенные фото

Уже есть warnings.

**Идея:** не слать combined с «полным» вердиктом по дефицитам, если `analysis_complete=false`; в письме явный блок «питание неполное, рекомендации отложены».

**Статус:** не начато

---

### 12. Weekly nutrition rollup

Из food-архивов: средние kcal, белок, клетчатка за неделю — таблица в письме + FACT для Gemini.

**Статус:** не начато

---

## Продукт

### 13. Чат с контекстом

В README — «чат с агентом», в API пока нет.

**Идея:** RAG по GCS (`archive/`, `outcomes/`) + `general` / `medical_data` из Drive.

**Ограничения free tier:** лимит сообщений, только последние N дней архивов.

**Статус:** не начато

---

### 14. Страница «статус»

`/status` или `/internal/status`: последний успешный daily/weekly, дата архива, версия деплоя, последняя ошибка Gemini.

**Зачем:** удобнее, чем копать JSON ответа cron.

**Статус:** не начато

---

## Качество кода

### 15. Тесты на критичное

Минимальный набор:

- `format_sleep_duration`;
- парсер weekly FACT-digest;
- `_morning_report_metrics_by_day` / фильтр сна.

**Статус:** не начато

---

### 16. Разбить `main.py`

~1200 строк: daily + weekly + probes → `routes/daily.py`, `routes/weekly.py`, helpers.

**Статус:** не начато

---

### 17. Синхронизировать README с реальностью

README ещё описывает Drive Shorts/Detailed как основное хранилище; фактически — GCS (`archive/`, `outcomes/`).

**Статус:** частично (weekly/cron обновлены)

---

## Безопасность

### 18. Усилить `/internal/*`

Сейчас: `CRON_SECRET` + публичный Cloud Run.

**Идея:** Scheduler с OIDC (SA → Cloud Run invoker) + секрет как второй слой; ротация `CRON_SECRET`.

**Статус:** не начато

---

## Рекомендуемый порядок внедрения

| # | Задача | Зачем |
|---|--------|--------|
| ~~1~~ | ~~Алерт при сбое daily/weekly~~ | ✓ сделано |
| ~~2~~ | ~~GCP monitoring без дублирования~~ | ✓ сделано |
| 3 | `sleep_metrics` в frontmatter при daily save | точнее weekly |
| 4 | Rollup + таблица 7 дней в weekly email | ценность при FACT-digest |
| 5 | Weekly MD в GCS | monthly и чат |

---

## Связанные решения (зафиксировано)

| Тема | Решение |
|------|---------|
| GCS | Полные expert MD в `archive/`; сырой Garmin JSON не пишем (с 2026-06-13) |
| Garmin fetch | D−1 полный (без sleep_data); D — только `sleep_data` |
| Weekly контекст | Сначала полные архивы; при >220k токенов — FACT-digest + disclaimer в письме |
| Weekly cron | На 1 ч позже daily (напр. вс 10:00 vs daily 09:00, Europe/Dublin) |
| Gemini free tier | 250k TPM = вход + выход; weekly бюджетировать `вход + max_output` |
| Сбои | П.1 email в приложении; п.2 GCP только если письмо не ушло / Scheduler failed; HTTP 200 после успешного failure-alert |

---

## Заметки для обсуждения

<!-- Добавляй сюда комментарии по ходу: -->

- 
