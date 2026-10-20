import json, math, pathlib, statistics, sys, unicodedata
rows = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))

FEATURES = ["кириллица", "латиница", "цифры", "пробелы", "переводы строк", "прочие символы", "не-ASCII прочее"]
def feats(text):
    cyr = lat = dig = sp = nl = oth = nonascii = 0
    for ch in text:
        if "Ѐ" <= ch <= "ӿ": cyr += 1
        elif ch.isascii() and ch.isalpha(): lat += 1
        elif ch.isdigit(): dig += 1
        elif ch in " \t": sp += 1
        elif ch in "\r\n": nl += 1
        elif ch.isascii(): oth += 1
        else: nonascii += 1
    return [cyr, lat, dig, sp, nl, oth, nonascii]

X = [feats(r["text"]) for r in rows]; y = [r["tokens"] for r in rows]
def nnls(X, y, iters=4000):
    """Неотрицательные МНК по относительной ошибке: координатный спуск."""
    n, m = len(X), len(X[0]); w = [1.0 / max(v, 1) for v in y]
    beta = [0.25] * m
    for _ in range(iters):
        for j in range(m):
            num = den = 0.0
            for i in range(n):
                resid = y[i] - sum(beta[k] * X[i][k] for k in range(m) if k != j)
                num += w[i] ** 2 * X[i][j] * resid; den += w[i] ** 2 * X[i][j] ** 2
            if den: beta[j] = max(0.0, num / den)
    return beta
def predict(beta, f): return sum(b * v for b, v in zip(beta, f))

def old_est(text):
    ascii_chars = sum(1 for ch in text if ch < "\x80"); return math.ceil(ascii_chars / 3.6 + (len(text) - ascii_chars) / 2.0)
rel = lambda p, t: p / t - 1

beta = nnls(X, y)
print("коэффициенты (токенов на символ класса):")
for name, b in zip(FEATURES, beta): print(f"   {name:18} {b:.3f}  (= 1 токен на {1 / b:.1f} симв.)" if b > 1e-6 else f"   {name:18} 0")
# leave-one-out
loo = []
for i in range(len(rows)):
    b = nnls(X[:i] + X[i + 1:], y[:i] + y[i + 1:], iters=800)
    loo.append(rel(predict(b, X[i]), y[i]))
fit = [rel(predict(beta, X[i]), y[i]) for i in range(len(rows))]
old = [rel(old_est(r["text"]), r["tokens"]) for r in rows]
def summary(name, errs):
    a = sorted(abs(e) for e in errs)
    print(f"{name:34} медиана |ошибки| {statistics.median(a):5.1%}  90-й перцентиль {a[int(len(a) * 0.9)]:5.1%}  худшая {a[-1]:5.1%}  смещение {statistics.mean(errs):+.1%}")
summary("старая оценка", old); summary("новая (на обучающих)", fit); summary("новая (leave-one-out)", loo)
tot_true = sum(y); print(f"\nСумма по всем образцам: реально {tot_true}; старая {sum(old_est(r['text']) for r in rows)} ({sum(old_est(r['text']) for r in rows) / tot_true - 1:+.0%}); новая {sum(predict(beta, f) for f in X):.0f} ({sum(predict(beta, f) for f in X) / tot_true - 1:+.0%})")
print("\nхудшие образцы новой модели (leave-one-out):")
for i in sorted(range(len(rows)), key=lambda i: -abs(loo[i]))[:5]: print(f"   {rows[i]['name']:34} реально {y[i]:5} оценка {predict(beta, X[i]):6.0f} ({loo[i]:+.0%})")
print("\nJSON коэффициентов:", json.dumps([round(b, 4) for b in beta]))
