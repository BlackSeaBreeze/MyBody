"""
Измеряет размер промпта /garmin/analyze в токенах и прикидывает стоимость.
Использует тот же контекст, что и backend (fetch_all_metrics + _metrics_to_text).

Запуск: .\\.venv\\Scripts\\python.exe scripts\\measure_tokens.py [days ...]
По умолчанию days = 1, 2, 7.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from backend import garmin_client, gemini_client

MODEL = "gemini-3-flash-preview"
# Прикидка по тарифам Flash (Paid Tier), $/1M токенов — для оценки порядка величины.
PRICE_IN_PER_M = 1.50
PRICE_OUT_PER_M = 9.00
ASSUMED_OUTPUT_TOKENS = 800


def main() -> int:
    days_list = [int(a) for a in sys.argv[1:]] or [1, 2, 7]

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    client = None
    if api_key:
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
        except Exception as e:
            print(f"(count_tokens недоступен: {e}) — покажу только символы.\n")

    for days in days_list:
        metrics = garmin_client.fetch_all_metrics(days=days)
        if not metrics.get("ok"):
            print(f"days={days}: данные Garmin недоступны: {metrics.get('error')}")
            continue
        text, meta = gemini_client.build_prompt_context(metrics)
        chars = len(text)
        level = meta.get("compaction_level")

        real_tokens = None
        if client is not None:
            try:
                ct = client.models.count_tokens(model=MODEL, contents=text)
                real_tokens = getattr(ct, "total_tokens", None)
            except Exception as e:
                print(f"  (count_tokens ошибка: {e})")
        if real_tokens is not None:
            cost_in = real_tokens / 1_000_000 * PRICE_IN_PER_M
            cost_out = ASSUMED_OUTPUT_TOKENS / 1_000_000 * PRICE_OUT_PER_M
            fits = "✓ в бесплатном слое" if real_tokens <= 250_000 else "✗ превышает лимит"
            print(
                f"days={days}: уровень ужатия={level}, символов={chars:,}, "
                f"ТОКЕНОВ(вход)={real_tokens:,} [{fits}] | стоимость≈ ${cost_in + cost_out:.4f}"
            )
        else:
            print(
                f"days={days}: уровень ужатия={level}, символов={chars:,}, "
                f"~токенов(оценка)={meta.get('est_tokens'):,}"
            )

    print(f"\nFree Tier лимит входных токенов: 250,000 / минуту на модель.")
    print(f"Тариф для оценки: ${PRICE_IN_PER_M}/1M вход, ${PRICE_OUT_PER_M}/1M выход (Flash, ориентир).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
