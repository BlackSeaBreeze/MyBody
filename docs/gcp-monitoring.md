# GCP Monitoring — без дублирования с email-алертом (roadmap п.1)

Два уровня уведомлений дополняют друг друга:

| Уровень | Когда срабатывает | Канал |
|---------|-------------------|--------|
| **П.1 — приложение** | Запрос дошёл до Cloud Run, отчёт не отправлен → письмо «сбой дневного/недельного отчёта» | SMTP (`MAIL_TO`) |
| **П.2 — GCP** | Письмо из п.1 **не удалось** отправить, или Scheduler **не получил 2xx**, или job **не запустился** | Cloud Monitoring → email (опц.) |

## Как избегаем двух писем об одном сбое

1. Если failure-alert из приложения **успешно отправлен**, endpoint возвращает **HTTP 200** (в JSON по-прежнему `"ok": false`). Cloud Scheduler считает job успешным → **нет** GCP-алерта «failed execution».
2. GCP **не** настраивается на «любой 5xx Cloud Run» для `/internal/daily-report` — это дублировало бы п.1.
3. GCP ловит **дыры**:
   - **Scheduler `attempt_failed`** — timeout, 502/503, когда приложение не смогло уведомить (SMTP упал, crash до alert).
   - **Лог `MYBODY_SAFETY_NET`** — pipeline упал, а failure-alert не ушёл (см. `backend/main.py`).
   - *(опционально)* отсутствие успешного запуска >26 ч — cron молча не сработал.

## Установка

Из корня репозитория (нужен `gcloud` и роль Monitoring Admin):

```powershell
.\scripts\setup_gcp_alerts.ps1 `
  -Project mybody-dev-env `
  -AlertEmail "your@gmail.com" `
  -DailyJobName mybody-daily-report `
  -WeeklyJobName mybody-weekly-report
```

Без `-AlertEmail` скрипт создаст политики и метрику, но выведет инструкцию привязать notification channel вручную в [Cloud Console → Monitoring → Alerting](https://console.cloud.google.com/monitoring/alerting).

## Что создаётся

| Имя | Тип |
|-----|-----|
| `mybody-safety-net` | Log-based metric (`MYBODY_SAFETY_NET` в логах Cloud Run) |
| `MyBody — safety net (failure email not sent)` | Alert policy |
| `MyBody — Scheduler daily failed` | Alert policy (`mybody-daily-report`) |
| `MyBody — Scheduler weekly failed` | Alert policy (`mybody-weekly-report`) |

## Проверка safety net

Симулировать сбой без SMTP (локально или временно убрать `SMTP_PASSWORD` в тестовом сервисе), вызвать:

```bash
curl -X POST "https://YOUR_SERVICE/internal/daily-report?send=true" -H "X-Cron-Secret: ..."
```

В логах Cloud Run должна появиться строка `MYBODY_SAFETY_NET`. Через несколько минут сработает политика monitoring (если настроен канал).

## Связанные env (приложение)

| Переменная | Описание |
|------------|----------|
| `PIPELINE_FAILURE_ALERTS` | `true` — п.1 включён |
| `CLOUD_RUN_LOGS_URL` | ссылка на логи в письме п.1 |

## Отключение

- П.1: `PIPELINE_FAILURE_ALERTS=false`
- П.2: удалить политики в Console или `gcloud monitoring policies delete <ID>`
