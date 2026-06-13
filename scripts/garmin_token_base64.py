"""
Экспорт токена Garmin (garth) в base64-строку для Secret Manager.

Берёт уже сохранённую локальную сессию из .garmin-tokens (созданную при удачном
входе локально) и печатает base64-строку. Если локальных токенов нет — выполняет
вход по GARMIN_EMAIL/GARMIN_PASSWORD из .env (один SSO-вход).

Запуск:
    .\\.venv\\Scripts\\python.exe scripts\\garmin_token_base64.py

Полученную строку целиком (одной строкой, без переносов) сохраните как значение
секрета `garmin-tokens-base64` в Secret Manager. Токен действует долго (обычно ~год),
но при смене пароля Garmin его нужно пересоздать.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from backend import garmin_client


def main() -> int:
    api, err, detail = garmin_client.get_client()
    if api is None:
        print(f"Не удалось войти в Garmin: {err}\n{detail or ''}", file=sys.stderr)
        return 1

    client = getattr(api, "garth", None) or getattr(api, "client", None)
    if client is None or not hasattr(client, "dumps"):
        print("Не найден объект garth с методом dumps().", file=sys.stderr)
        return 2

    token_b64 = client.dumps()
    print(token_b64)
    print(f"\n# длина: {len(token_b64)} символов — скопируйте строку выше целиком (от {{ до }})")
    print("# Secret Manager → garmin-tokens-base64")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
