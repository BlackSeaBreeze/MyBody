"""Count tokens in a text/markdown file (sections + encoders)."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHARS_PER_TOKEN = 1.35  # MyBody gemini_client default


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ["TEMP"]) / "vb-20260613-1108.md"
    text = path.read_text(encoding="utf-8")
    chars = len(text)
    bytes_ = path.stat().st_size
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)

    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    gpt_tokens = len(enc.encode(text))
    try:
        o200k_tokens = len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        o200k_tokens = None

    sections: dict[str, str] = {}
    for p in re.split(r"^## ", text, flags=re.M)[1:]:
        name = p.split("\n", 1)[0].strip()
        body = p.split("\n", 1)[1] if "\n" in p else ""
        sections[name] = body

    meta: dict = {}
    m = re.search(r"```json\n(\{.*?\})\n```", text, re.S)
    if m:
        try:
            meta = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    print("=== FILE ===")
    print(f"path: {path}")
    print(f"bytes: {bytes_:,}")
    print(f"chars: {chars:,}")
    print(f"lines: {lines:,}")
    print(f"words (whitespace split): {len(text.split()):,}")

    print("\n=== TOKENS ===")
    print(f"MyBody estimate (chars/1.35): {int(chars / CHARS_PER_TOKEN):,}")
    print(f"tiktoken cl100k_base: {gpt_tokens:,}")
    if o200k_tokens is not None:
        print(f"tiktoken o200k_base: {o200k_tokens:,}")

    print("\n=== METADATA (first JSON block) ===")
    print(f"input_est_tokens: {meta.get('input_est_tokens')}")
    print(f"model: {meta.get('model')}")
    print(f"compaction_level: {meta.get('compaction_level')}")

    print("\n=== SECTIONS ===")
    for name, body in sections.items():
        c = len(body)
        t = len(enc.encode(body))
        print(f"  {name[:55]:55} | {c:>8,} ch | ~{int(c/CHARS_PER_TOKEN):>7,} | {t:>7,} tok")

    json_blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    json_text = "\n".join(json_blocks)
    print(f"\nJSON in fences: {len(json_blocks)} blocks, {len(json_text):,} chars, {len(enc.encode(json_text)):,} cl100k")

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            from google import genai

            client = genai.Client(api_key=api_key)
            for model in ("gemini-2.5-flash", "gemini-2.0-flash"):
                try:
                    ct = client.models.count_tokens(model=model, contents=text)
                    total = getattr(ct, "total_tokens", None)
                    print(f"Gemini count_tokens ({model}): {total:,}")
                except Exception as e:
                    print(f"Gemini count_tokens ({model}): {e}")
        except Exception as e:
            print(f"Gemini client: {e}")
    else:
        print("GEMINI_API_KEY not set — skip Gemini count_tokens")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
