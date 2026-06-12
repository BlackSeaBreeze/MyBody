"""
Проверка входа Garmin тем же путём, что и API MyBody (garminconnect → garth).
Запуск из корня репозитория с активированным venv:

  python scripts/garmin_sso_probe.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def _print_http_from_chain(exc: BaseException) -> None:
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        resp = getattr(cur, "response", None)
        if resp is not None and getattr(resp, "status_code", None) is not None:
            print(f"  HTTP {resp.status_code}  {resp.url}")
            text = (getattr(resp, "text", None) or "")[:800]
            if text.strip():
                print(f"  Body (prefix):\n{text}")
            return
        err = getattr(cur, "error", None)
        if err is not None:
            resp = getattr(err, "response", None)
            if resp is not None and getattr(resp, "status_code", None) is not None:
                print(f"  HTTP {resp.status_code}  {resp.url}")
                text = (getattr(resp, "text", None) or "")[:800]
                if text.strip():
                    print(f"  Body (prefix):\n{text}")
                return
        nxt = cur.__cause__
        if nxt is None and cur.__context__ is not None and cur.__context__ is not cur:
            nxt = cur.__context__
        cur = nxt if isinstance(nxt, BaseException) else None


def main() -> int:
    try:
        from garminconnect import Garmin
    except ImportError:
        print("Нет пакета garminconnect. Установите: pip install -r backend/requirements.txt", file=sys.stderr)
        return 1

    email = os.environ.get("GARMIN_EMAIL", "").strip()
    password = os.environ.get("GARMIN_PASSWORD", "").strip()
    if not email or not password:
        print("Задайте GARMIN_EMAIL и GARMIN_PASSWORD в .env (корень проекта).", file=sys.stderr)
        return 1

    print("Тот же стек, что у /garmin/*: Garmin(...).login() → garth SSO (GET embed, GET signin, POST signin).")
    masked = email[:2] + "***@" + email.split("@", 1)[-1] if "@" in email else "(hidden)"
    print(f"User-Agent сессии: как у garth Client (см. пакет). Учётка: {masked}\n")

    try:
        api = Garmin(email, password)
        api.login()
    except Exception as e:
        print(f"Итог: ОШИБКА — {type(e).__name__}: {e}")
        _print_http_from_chain(e)
        return 1

    print("Итог: УСПЕХ — OAuth-сессия получена.")
    print(f"  display_name: {getattr(api, 'display_name', None)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
