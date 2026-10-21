"""How many tokens the loaded models really count (usage.prompt_tokens) against the harness's estimate. The requests are tiny."""
import json, sys, httpx
from core.files import estimate_tokens

URL = "http://127.0.0.1:1234/v1/chat/completions"
RU = ("Привет! Это пример обычного русского текста для измерения числа токенов. Мы проверяем, как токенизатор модели "
# RU stays Russian: it measures the Cyrillic coefficient
      "справляется с кириллицей, знаками препинания и длинными словами вроде «переопределение» и «токенизатор». ") * 8
EN = ("Hello! This is a plain English paragraph used to measure how many tokens the tokenizer produces for ordinary prose, "
      "including punctuation and a few longer words such as internationalization and tokenization. ") * 8
CODE = ("def add(a, b):\n    result = a + b  # sum\n    return {'value': result, 'ok': True}\n\n"
        "for i in range(10):\n    print(f'item {i}: {add(i, 2)}')\n") * 10
JSONT = json.dumps({"name": "toolbox.fs_read", "description": "Reads a text file", "arguments_schema": {"type": "object",
        "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer", "default": 20000}}, "required": ["path"]}},
        ensure_ascii=False, indent=1) * 6
SAMPLES = {"Russian": RU, "English": EN, "code": CODE, "JSON (English)": JSONT}

def prompt_tokens(model, text):
    body = {"model": model, "stream": False, "max_tokens": 1, "temperature": 0,
            "messages": [{"role": "user", "content": text}]}
    r = httpx.post(URL, json=body, timeout=180, trust_env=False); r.raise_for_status()
    return r.json()["usage"]["prompt_tokens"]

for model in sys.argv[1:]:
    base = prompt_tokens(model, "ok")        # chat template overhead
    print(f"\n## {model}: chat template ≈{base} tok.")
    for name, text in SAMPLES.items():
        real = prompt_tokens(model, text) - base
        est = estimate_tokens(text)
        print(f"  {name:15} {len(text):6} chars -> {real:5} tok. ({len(text) / real:4.2f} chars/tok.)  harness estimate {est:5}  estimate error {est / real - 1:+.0%}")
