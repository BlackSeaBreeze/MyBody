# MyBody

Веб-сервис для персональных рекомендаций по здоровью: питание, сон, активность. Ежедневная сводка по email и чат с ИИ-агентом на базе Gemini. Данные (фото еды, витамины, активность) вносятся в приложение один раз и используются и для отчёта, и для чата.

## Деплой в Google Cloud Run (через GitHub)

При пуше в ветку **`dev`** (или `main`) проект автоматически собирается и деплоится в Cloud Run. Рабочая ветка — **`dev`**.

### 1. Подготовка проекта в Google Cloud

Проект: **myBody-dev** (ID: `mybody-dev-env`) — [консоль GCP](https://console.cloud.google.com/welcome?project=mybody-dev-env).

Используется **Workload Identity Federation**: в GitHub не хранятся ключи, только идентификаторы проекта; доступ по короткоживущим токенам через GitHub OIDC.

**1. Включите API** в [консоли GCP](https://console.cloud.google.com/apis/library?project=mybody-dev-env):

- Cloud Run API  
- Artifact Registry API  
- Cloud Build API  
- IAM API  
- Security Token Service API (STS)  
- [Единая ссылка на включение](https://console.cloud.google.com/flows/enableapi?apiid=run.googleapis.com,artifactregistry.googleapis.com,cloudbuild.googleapis.com,iam.googleapis.com,sts.googleapis.com&project=mybody-dev-env)

**2. Репозиторий Artifact Registry**

- [Artifact Registry → Create repository](https://console.cloud.google.com/artifacts?project=mybody-dev-env): имя **`mybody`**, формат Docker, регион **`europe-west1`**.

**3. Сервисный аккаунт для деплоя**

- [Service accounts → Create](https://console.cloud.google.com/iam-admin/serviceaccounts?project=mybody-dev-env): имя **`github-actions-deploy`**.
- Роли: **Cloud Run Admin**, **Artifact Registry Writer**, **Service Account User** (образ собирается в раннере GitHub Actions и пушится в Artifact Registry — доступ к Cloud Storage не нужен).
- Ключ создавать не нужно — доступ будет через WIF.

**4. Workload Identity Pool и провайдер (один раз)**

В [Cloud Shell](https://console.cloud.google.com/?cloudshell=true) или локально с `gcloud auth application-default login` выполните (подставьте свой **Project ID** и **номер проекта**; номер можно посмотреть в [настройках проекта](https://console.cloud.google.com/iam-admin/settings?project=mybody-dev-env) или командой `gcloud projects describe mybody-dev-env --format='value(projectNumber)'`):

```bash
export PROJECT_ID=mybody-dev-env
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')

# Пул
gcloud iam workload-identity-pools create github-actions-pool \
  --project="$PROJECT_ID" \
  --location=global \
  --display-name="GitHub Actions"

# Провайдер (Trust GitHub OIDC; ограничение по репозиторию — подставьте свой owner/repo)
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project="$PROJECT_ID" \
  --location=global \
  --workload-identity-pool=github-actions-pool \
  --display-name="GitHub" \
  --issuer-uri="https://token.actions.githubusercontent.com/" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner=='BlackSeaBreeze' && assertion.repository=='BlackSeaBreeze/MyBody'"
```

**5. Разрешить репозиторию использовать сервисный аккаунт**

Тот же `PROJECT_ID` и `PROJECT_NUMBER`:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  github-actions-deploy@${PROJECT_ID}.iam.gserviceaccount.com \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions-pool/attribute.repository/BlackSeaBreeze/MyBody"
```

Если репозиторий другой — замените `BlackSeaBreeze/MyBody` на свой `OWNER/REPO` в `--attribute-condition` и в `--member`.

### 2. Переменные в GitHub (без секретов)

В настройках репозитория: **Settings → Secrets and variables → Actions → Variables** добавьте:

| Переменная             | Значение          | Пример        |
|------------------------|-------------------|---------------|
| `GCP_PROJECT_ID`       | ID проекта GCP    | `mybody-dev-env` |
| `GCP_PROJECT_NUMBER`   | Номер проекта     | см. в консоли или `gcloud projects describe mybody-dev-env --format='value(projectNumber)'` |

Ключи в GitHub не нужны — аутентификация идёт через Workload Identity Federation.

После этого каждый пуш в **`dev`** или **`main`** запускает сборку и деплой.

### 3. Почистка (опционально)

Если раньше настраивали деплой через Cloud Build и GCS, можно убрать лишнее:

- **GitHub:** переменную **`GCS_STAGING_BUCKET`** (Settings → Variables) — удалить, если есть.
- **GCP, сервисный аккаунт `github-actions-deploy`:** снять роли **Storage Admin** и **Storage Object Admin** с проекта (IAM → выберить SA → Edit → убрать эти роли). Для текущего деплоя они не нужны.
- **GCP, бакет `mybody-dev-env-build-source`:** если создавали для загрузки исходников и он больше не нужен — [удалить бакет](https://console.cloud.google.com/storage/browser?project=mybody-dev-env) (сначала удалить объекты внутри).

### 4. Регион

В [.github/workflows/deploy.yml](.github/workflows/deploy.yml) по умолчанию указан регион `europe-west1`. Если нужен другой — измените переменную `REGION` и создайте репозиторий Artifact Registry в этом же регионе.

## Локальный запуск

Рекомендуется использовать виртуальное окружение. Из **корня проекта**:

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload
```

**Windows (cmd) / Linux / macOS:**
```bash
python -m venv .venv
.venv\Scripts\activate          # Windows cmd
# source .venv/bin/activate     # Linux/macOS
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload
```

API: <http://localhost:8000>  
Проверка: <http://localhost:8000/health>

## Garmin Connect (прямое подключение)

Данные с Garmin (активность, сон, статистика) забираются через библиотеку [garminconnect](https://pypi.org/project/garminconnect/).

### Переменные окружения

| Переменная | Описание |
|------------|----------|
| `GARMIN_EMAIL` | Email от аккаунта Garmin Connect |
| `GARMIN_PASSWORD` | Пароль от аккаунта Garmin Connect |
| `CRON_SECRET` | (Опционально.) Секрет для вызова эндпоинта загрузки (заголовок `X-Cron-Secret`) |

### Логин и пароль в Secret Manager (Cloud Run)

1. **Создать секреты в GCP**  
   [Secret Manager → Create secret](https://console.cloud.google.com/security/secret-manager?project=mybody-dev-env):
   - Имя: например **`garmin-email`**, значение — твой email.
   - Имя: например **`garmin-password`**, значение — пароль от Garmin Connect.

2. **Подключить к сервису Cloud Run**  
   [Cloud Run → сервис mybody → Edit & deploy new revision](https://console.cloud.google.com/run?project=mybody-dev-env):
   - Вкладка **Variables & Secrets** → **Add variable**:
     - **GARMIN_EMAIL** → выбери **Reference a secret** → секрет `garmin-email`, версия `latest`.
     - **GARMIN_PASSWORD** → **Reference a secret** → секрет `garmin-password`, версия `latest`.
   - При необходимости добавь **CRON_SECRET** так же через Reference a secret.
   - Нажми **Deploy**.

   **Либо** задать секреты из кода: в [.github/workflows/deploy.yml](.github/workflows/deploy.yml) при деплое уже прописано `--set-secrets=GARMIN_EMAIL=garmin-email:latest,GARMIN_PASSWORD=garmin-password:latest` — при каждой пересборке эти переменные подхватываются из Secret Manager. Имена секретов в GCP должны совпадать: `garmin-email`, `garmin-password`.

3. **Права доступа**  
   Сервисный аккаунт Cloud Run (по умолчанию `PROJECT_NUMBER-compute@developer.gserviceaccount.com`) должен иметь роль **Secret Manager Secret Accessor** на секреты `garmin-email` и `garmin-password`. [IAM → выдать роль](https://console.cloud.google.com/iam-admin/iam?project=mybody-dev-env) или в карточке каждого секрета → **Permissions** → Add principal → этот аккаунт, роль Secret Manager Secret Accessor.

После деплоя сервис будет читать логин и пароль из Secret Manager; в логах и конфигурации значения видны не будут.

### Эндпоинты

- **GET /garmin/status** — проверка, заданы ли учётные данные (без логина в Garmin).
- **GET /garmin/data?days=7** — данные за последние дни: активности и статистика по дням (удобно открыть в браузере и посмотреть, что приходит из Garmin). Параметр `days` от 1 до 31.
- **POST /internal/garmin-fetch?days=1** — то же для вызова по расписанию. Если задан **CRON_SECRET**, в запросе обязателен заголовок **`X-Cron-Secret`** с тем же значением.

Для ежедневной выгрузки настройте **Cloud Scheduler**: HTTP-запрос на `https://YOUR_SERVICE_URL/internal/garmin-fetch?days=1` с заголовком `X-Cron-Secret: <CRON_SECRET>`.

## Дальнейшие шаги

- Подключение Gemini API (дневной отчёт и чат)
- Чтение данных из Google Drive / таблицы
- Отправка ежедневного отчёта по email
- Веб-интерфейс чата с агентом
