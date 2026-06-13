"""
Проверка ключа Gemini (GEMINI_API_KEY) без данных Garmin.

- Подтверждает, что ключ задан и принят Google.
- Печатает несколько доступных моделей.
- Делает один крошечный запрос к модели по умолчанию из gemini_client.

Запуск (из корня репозитория):
  .\\.venv\\Scripts\\python.exe scripts\\gemini_check.py
  .\\.venv\\Scripts\\python.exe scripts\\gemini_check.py gemini-3-flash-preview   # своя модель
"""
from __future__ import annotations

import os
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

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

DEFAULT_MODEL = "gemini-3-flash-preview"


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("GEMINI_API_KEY не задан в .env (корень проекта).")
        return 1
    masked = api_key[:6] + "…" + api_key[-4:] if len(api_key) > 12 else "(скрыт)"
    print(f"Ключ найден: {masked}")

    try:
        from google import genai
    except ImportError:
        print("Пакет google-genai не установлен. pip install -r backend/requirements.txt")
        return 1

    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL

    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        print(f"Не удалось создать клиент: {e}")
        return 1

    # 1) Список моделей — проверяет, что ключ валиден (без расхода токенов).
    try:
        names = []
        for m in client.models.list():
            n = getattr(m, "name", "")
            if n:
                names.append(n.replace("models/", ""))
        print(f"\nКлюч принят. Доступно моделей: {len(names)}.")
        preview = [n for n in names if "gemini" in n][:12]
        for n in preview:
            print(f"  - {n}")
    except Exception as e:
        print(f"\nКлюч НЕ принят при запросе списка моделей: {e}")
        return 1

    # 2) Крошечный запрос к выбранной модели (минимальный расход токенов).
    print(f"\nТест генерации (модель: {model})...")
    try:
        resp = client.models.generate_content(
            model=model,
            contents="Ответь одним словом: ok",
        )
        text = (getattr(resp, "text", None) or "").strip()
        if text:
            print(f"Ответ модели: {text!r}")
            print("\nИтог: Gemini настроен и работает.")
            return 0
        print("Пустой ответ модели (ключ валиден, но генерация не вернула текст).")
        return 1
    except Exception as e:
        print(f"Ошибка генерации: {e}")
        print("Ключ, вероятно, валиден (список моделей получен), но модель недоступна — "
              "попробуйте другое имя модели, напр. gemini-2.0-flash.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
