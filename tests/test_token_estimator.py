"""The token estimator: accuracy on real measurements, edge cases, speed."""
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
print(f"      {len(rows)} samples: median |error| {statistics.median(abs_errs):.1%}, p90 {abs_errs[int(len(abs_errs) * 0.9)]:.1%}, worst {abs_errs[-1]:.1%}, total bias {total_est / total_true - 1:+.1%}")
check("median error ≤ 8%", statistics.median(abs_errs) <= 0.08)
check("90th percentile ≤ 20%", abs_errs[int(len(abs_errs) * 0.9)] <= 0.20)
check("worst error ≤ 40%", abs_errs[-1] <= 0.40)
check("total bias within ±3% (no underestimate on average)", abs(total_est / total_true - 1) <= 0.03)

# different kinds of text measured on all three models (tokcal): Russian ≈4 chars/token, English ≈5.8, code ≈2.5.
# The samples are measured data: the Russian prose and the Russian code comment stay as they were measured.
RU = ("Привет! Это пример обычного русского текста для измерения числа токенов. Мы проверяем, как токенизатор модели "
      "справляется с кириллицей, знаками препинания и длинными словами вроде «переопределение» и «токенизатор». ") * 8
EN = ("Hello! This is a plain English paragraph used to measure how many tokens the tokenizer produces for ordinary prose, "
      "including punctuation and a few longer words such as internationalization and tokenization. ") * 8
CODE = ("def add(a, b):\n    result = a + b  # сумма\n    return {'value': result, 'ok': True}\n\n"
        "for i in range(10):\n    print(f'item {i}: {add(i, 2)}')\n") * 10
for name, text, real in (("Russian prose", RU, 425), ("English prose", EN, 287), ("code", CODE, 558)):
    e = estimate_tokens(text) / real - 1
    check(f"{name}: actually {real}, estimate {estimate_tokens(text)} ({e:+.0%})", abs(e) <= 0.35, e)
check("code is underestimated by no more than 20% (the margin and the server correction cover the rest)", estimate_tokens(CODE) / 558 - 1 > -0.20)

check("empty string", estimate_tokens("") == 0)
check("one character does not give 0", estimate_tokens("a") >= 1)
check("monotonic: a longer text does not give fewer tokens", estimate_tokens("hello " * 10) < estimate_tokens("hello " * 20))
check("emoji and CJK do not crash and are not zero", estimate_tokens("😀😀😀") > 0 and estimate_tokens("你好世界") > 0)
check("digits cost more than letters (Cyrillic letters here)", estimate_tokens("1234567890") > estimate_tokens("абвгдежзик"))
big = "Строка с кириллицей и digits 123.\n" * 100_000   # mixed Cyrillic and Latin, as in the measured data
t0 = time.perf_counter(); n = estimate_tokens(big); dt = time.perf_counter() - t0
print(f"      {len(big):,} characters: {n:,} tok. in {dt * 1000:.0f} ms")
check("3.4 million characters are counted in under 0.5 s", dt < 0.5, dt)
print("ALL OK" if ok_all else "SOME FAILED")

