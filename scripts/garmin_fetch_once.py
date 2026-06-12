"""
Один аккуратный запрос данных Garmin (как в API /garmin/data, но только за 1 день).

- Один вызов fetch_recent_data(days=1) → внутри один get_client() (логин по токенам или одна SSO-цепочка).
- Логи garminconnect/garth приглушены, чтобы не засорять консоль.

Запуск (из корня репозитория, один раз):

  .\\.venv\\Scripts\\python.exe scripts\\garmin_fetch_once.py

Не запускайте подряд много раз — каждый запуск = новая сессия клиента.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)
logging.getLogger("garth").setLevel(logging.CRITICAL)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def main() -> int:
    from backend import garmin_client

    store = garmin_client._garmin_tokenstore_dir()
    cached = garmin_client._garmin_tokens_on_disk(store)
    print("Каталог токенов:", store)
    print(
        "Сохранённая сессия (garmin_tokens.json / старые oauth):",
        "да" if cached else "нет — будет полный вход",
    )
    print("Запрос: только последние 1 сутки данных (один проход)...\n")

    data = garmin_client.fetch_recent_data(days=1)

    if data.get("detail"):
        print("Сообщение от библиотеки (detail):", data["detail"], "\n")

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > 12000:
        text = text[:12000] + "\n… [обрезано]\n"
    print(text)

    err = data.get("error")
    if data.get("ok"):
        print("\nГотово: ok=true")
        return 0
    print(f"\nНе удалось: {err}")
    if err == "garmin_rate_limited":
        print("Не повторяйте запрос сразу — снова упрётесь в лимит Garmin.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
