"""Собирает пары (текст, реальное число токенов) с загруженной модели для подгонки оценщика. Запросы крошечные (<1200 токенов)."""
import json, pathlib, random, re, sys, httpx

URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = sys.argv[1]
OUT = pathlib.Path(sys.argv[2])
random.seed(7)

def chunks_of(path, size=1500, n=3):
    try: text = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError: return []
    if len(text) <= size: return [text] if text.strip() else []
    out = []
    for _ in range(n):
        i = random.randint(0, len(text) - size); out.append(text[i:i + size])
    return out

samples = []
root = pathlib.Path("C:/SaiHarness")
for p in ["TEST_PLAN.md", "core/files.py", "core/tools.py", "core/llm.py", "ui/main_window.py", "ui/chat_view.py", "tools_server.py",
          "worker.py", "requirements.txt", "data/crash.log", "ui/theme.qss", "scripts/make_test_sandbox.ps1", "core/richtext.py", "core/db.py"]:
    for c in chunks_of(root / p): samples.append((p, c))
for p in list((root / "data/notes").glob("*.md"))[:4]:
    for c in chunks_of(p, 1500, 2): samples.append((f"notes/{p.name}", c))
# синтетика: разные типы содержимого
samples += [
    ("синт: русская проза", "Вчера мы долго обсуждали, как лучше организовать работу над проектом, и в итоге решили, что каждый возьмёт на себя отдельный модуль. " * 10),
    ("синт: английская проза", "Yesterday we discussed at length how best to organise the work on the project and decided that everyone would take one module. " * 10),
    ("синт: числа и CSV", "\n".join(f"{i},{i * 37 % 1000},{i / 7:.4f},2026-10-{i % 28 + 1:02d}" for i in range(120))),
    ("синт: пути и логи", "\n".join(f"[2026-10-15 20:{i % 60:02d}:11] INFO C:\\Users\\alexm\\AppData\\Local\\Temp\\tmp{i}abc\\proj\\file{i}.txt: ok ({i * 13} bytes)" for i in range(30))),
    ("синт: таблица markdown", "| Модель | Размер | Скорость |\n|---|---|---|\n" + "\n".join(f"| модель-{i} | {i}B | {i * 3} ток/с |" for i in range(40))),
    ("синт: смесь ru+код", "Функция `calc` вычисляет выражение:\n```python\ndef calc(expr: str) -> str:\n    return str(eval(expr))\n```\nРезультат: 391.\n" * 8),
]
print("образцов:", len(samples))

def prompt_tokens(text):
    body = {"model": MODEL, "stream": False, "max_tokens": 1, "temperature": 0, "messages": [{"role": "user", "content": text}]}
    r = httpx.post(URL, json=body, timeout=240, trust_env=False); r.raise_for_status()
    return r.json()["usage"]["prompt_tokens"]

base = prompt_tokens("ok")
rows = []
for i, (name, text) in enumerate(samples):
    try: rows.append({"name": name, "text": text, "tokens": prompt_tokens(text) - base})
    except Exception as e: print("пропуск", name, e); continue
    if i % 10 == 0: print(f"  {i}/{len(samples)}", flush=True)
OUT.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
print("сохранено", len(rows), "образцов; всего токенов", sum(r["tokens"] for r in rows))
