# MyBody

Веб-сервис для персональных рекомендаций по здоровью: питание, сон, активность. Ежедневная сводка по email и чат с ИИ-агентом на базе Gemini. Данные (фото еды, витамины, активность) вносятся в приложение один раз и используются и для отчёта, и для чата.

## Деплой в Google Cloud Run (через GitHub)

При пуше в ветку **`dev`** (или `main`) проект автоматически собирается и деплоится в Cloud Run. Рабочая ветка — **`dev`**.

### 1. Подготовка проекта в Google Cloud

Проект: **myBody-dev** (ID: `mybody-dev-env`) — [консоль GCP](https://console.cloud.google.com/welcome?project=mybody-dev-env).

1. **Включите нужные API** в [консоли GCP](https://console.cloud.google.com/apis/library?project=mybody-dev-env):
   - Cloud Run API  
   - Artifact Registry API  
   - Cloud Build API  

2. **Создайте репозиторий в Artifact Registry** (один раз):
   - [Artifact Registry → Create repository](https://console.cloud.google.com/artifacts?project=mybody-dev-env)
   - Имя: `mybody`
   - Формат: Docker
   - Регион: `europe-west1` (или тот же, что в workflow)

3. **Сервисный аккаунт для GitHub Actions**:
   - [IAM → Service accounts → Create](https://console.cloud.google.com/iam-admin/serviceaccounts?project=mybody-dev-env)
   - Имя, например: `github-actions-mybody`
   - Роли: **Cloud Run Admin**, **Artifact Registry Writer**, **Service Account User**
   - Создайте ключ (JSON) и сохраните файл — он понадобится для секрета в GitHub.

### 2. Секреты в GitHub

В настройках репозитория: **Settings → Secrets and variables → Actions** добавьте:

| Секрет        | Значение |
|---------------|----------|
| `GCP_PROJECT_ID` | `mybody-dev-env` |
| `GCP_SA_KEY`     | Содержимое **всего** JSON-файла ключа сервисного аккаунта (одной строкой) |

После этого каждый пуш в **`dev`** (или `main`) будет запускать сборку и деплой.

### 3. Регион

В файле [.github/workflows/deploy.yml](.github/workflows/deploy.yml) по умолчанию указан регион `europe-west1`. Если нужен другой — измените переменную `REGION` и создайте репозиторий Artifact Registry в этом же регионе.

## Локальный запуск

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

API: <http://localhost:8000>  
Проверка: <http://localhost:8000/health>

## Дальнейшие шаги

- Подключение Gemini API (дневной отчёт и чат)
- Чтение данных из Google Drive / таблицы
- Отправка ежедневного отчёта по email
- Веб-интерфейс чата с агентом
