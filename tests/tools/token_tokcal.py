"""Сколько токенов реально насчитывают загруженные модели (usage.prompt_tokens) против оценки обвязки. Запросы крошечные."""
import json, sys, httpx
from core.files import estimate_tokens

URL = "http://127.0.0.1:1234/v1/chat/completions"
RU = ("Привет! Это пример обычного русского текста для измерения числа токенов. Мы проверяем, как токенизатор модели "
      "справляется с кириллицей, знаками препинания и длинными словами вроде «переопределение» и «токенизатор». ") * 8
EN = ("Hello! This is a plain English paragraph used to measure how many tokens the tokenizer produces for ordinary prose, "
      "including punctuation and a few longer words such as internationalization and tokenization. ") * 8
CODE = ("def add(a, b):\n    result = a + b  # сумма\n    return {'value': result, 'ok': True}\n\n"
        "for i in range(10):\n    print(f'item {i}: {add(i, 2)}')\n") * 10
JSONT = json.dumps({"name": "toolbox.fs_read", "description": "Читает текстовый файл", "arguments_schema": {"type": "object",
        "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer", "default": 20000}}, "required": ["path"]}},
        ensure_ascii=False, indent=1) * 6
SAMPLES = {"русский": RU, "английский": EN, "код": CODE, "JSON (русский)": JSONT}

def prompt_tokens(model, text):
    body = {"model": model, "stream": False, "max_tokens": 1, "temperature": 0,
            "messages": [{"role": "user", "content": text}]}
    r = httpx.post(URL, json=body, timeout=180, trust_env=False); r.raise_for_status()
    return r.json()["usage"]["prompt_tokens"]

for model in sys.argv[1:]:
    base = prompt_tokens(model, "ok")        # накладные расходы шаблона чата
    print(f"\n## {model}: шаблон чата ≈{base} ток.")
    for name, text in SAMPLES.items():
        real = prompt_tokens(model, text) - base
        est = estimate_tokens(text)
        print(f"  {name:15} {len(text):6} симв. -> {real:5} ток. ({len(text) / real:4.2f} симв./ток.)  оценка обвязки {est:5}  ошибка оценки {est / real - 1:+.0%}")
