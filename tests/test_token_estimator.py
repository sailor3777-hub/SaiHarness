"""Оценщик токенов: точность на реальных замерах, граничные случаи, скорость."""
import json, pathlib, statistics, time
from core.files import estimate_tokens

ok_all = True
def check(name, cond, extra=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

rows = json.loads((pathlib.Path(__file__).parent / "data" / "tokdata_qwen4b.json").read_text(encoding="utf-8"))
errs = [estimate_tokens(r["text"]) / r["tokens"] - 1 for r in rows if r["tokens"] >= 20]
abs_errs = sorted(abs(e) for e in errs)
total_true = sum(r["tokens"] for r in rows); total_est = sum(estimate_tokens(r["text"]) for r in rows)
print(f"      образцов {len(rows)}: медиана |ошибки| {statistics.median(abs_errs):.1%}, p90 {abs_errs[int(len(abs_errs) * 0.9)]:.1%}, худшая {abs_errs[-1]:.1%}, суммарное смещение {total_est / total_true - 1:+.1%}")
check("медиана ошибки ≤ 8%", statistics.median(abs_errs) <= 0.08)
check("90-й перцентиль ≤ 20%", abs_errs[int(len(abs_errs) * 0.9)] <= 0.20)
check("худшая ошибка ≤ 40%", abs_errs[-1] <= 0.40)
check("суммарное смещение в пределах ±3% (не занижает в среднем)", abs(total_est / total_true - 1) <= 0.03)

# разные типы, измеренные на всех трёх моделях (tokcal): русский ≈4 симв./ток., английский ≈5.8, код ≈2.5
RU = ("Привет! Это пример обычного русского текста для измерения числа токенов. Мы проверяем, как токенизатор модели "
      "справляется с кириллицей, знаками препинания и длинными словами вроде «переопределение» и «токенизатор». ") * 8
EN = ("Hello! This is a plain English paragraph used to measure how many tokens the tokenizer produces for ordinary prose, "
      "including punctuation and a few longer words such as internationalization and tokenization. ") * 8
CODE = ("def add(a, b):\n    result = a + b  # сумма\n    return {'value': result, 'ok': True}\n\n"
        "for i in range(10):\n    print(f'item {i}: {add(i, 2)}')\n") * 10
for name, text, real in (("русская проза", RU, 425), ("английская проза", EN, 287), ("код", CODE, 558)):
    e = estimate_tokens(text) / real - 1
    check(f"{name}: реально {real}, оценка {estimate_tokens(text)} ({e:+.0%})", abs(e) <= 0.35, e)
check("код занижается не сильнее, чем на 20% (остальное закрывают запас и поправка по замерам сервера)", estimate_tokens(CODE) / 558 - 1 > -0.20)

check("пустая строка", estimate_tokens("") == 0)
check("один символ не даёт 0", estimate_tokens("a") >= 1)
check("монотонность: длиннее текст не даёт меньше токенов", estimate_tokens("привет " * 10) < estimate_tokens("привет " * 20))
check("эмодзи и CJK не падают и не нули", estimate_tokens("😀😀😀") > 0 and estimate_tokens("你好世界") > 0)
check("цифры дороже букв", estimate_tokens("1234567890") > estimate_tokens("абвгдежзик"))
big = "Строка с кириллицей и digits 123.\n" * 100_000
t0 = time.perf_counter(); n = estimate_tokens(big); dt = time.perf_counter() - t0
print(f"      {len(big):,} символов: {n:,} ток. за {dt * 1000:.0f} мс")
check("3,4 млн символов считаются быстрее 0,5 с", dt < 0.5, dt)
print("ALL OK" if ok_all else "SOME FAILED")

