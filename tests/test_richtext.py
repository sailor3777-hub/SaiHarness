from core.richtext import simplify_latex, render_markdown

ok_all = True
def check(name, got, want=None, cond=None):
    global ok_all
    good = cond if cond is not None else got == want
    ok_all &= bool(good)
    print(("OK   " if good else "FAIL ") + name + ("" if good else f"\n       got : {got!r}\n       want: {want!r}"))

S = simplify_latex
check("пример пользователя", S(r"$58 - 29 = \mathbf{29}$"), "58 - 29 = **29**")
check("дробь", S(r"$\frac{1}{2}$"), "1/2")
check("дробь со сложным числителем", S(r"$\frac{a+b}{c}$"), "(a+b)/c")
check("степень", S(r"$x^2 + y^{10}$"), "x² + y¹⁰")
check("степень с буквой -> запасной вариант", S(r"$e^{-x}$"), "e^(-x)")
check("индекс", S(r"$a_1 + a_{12}$"), "a₁ + a₁₂")
check("корень", S(r"$\sqrt{16} = 4$"), "√(16) = 4")
check("символы", S(r"$3 \times 4 \cdot 2 \div 1 \neq \pi$"), "3 × 4 · 2 ÷ 1 ≠ π")
check("\\le не ломает \\left", S(r"$\left( x \le 5 \right)$"), "( x ≤ 5 )")
check("\\leftarrow", S(r"$a \leftarrow b$"), "a ← b")
check("\\in и \\int", S(r"$x \in A, \int f$"), "x ∈ A, ∫ f")
check("\\( \\) и \\[ \\]", S(r"\(a \times b\) и \[c \pm d\]"), "a × b и c ± d")
check("$$ блок $$", S("$$\n\\frac{a}{b}\n$$"), "a/b")
check("\\text внутри", S(r"$5 \text{ яблок}$"), "5  яблок")
check("деньги не формулы", S("Стоит $5 и $10 всего"), "Стоит $5 и $10 всего")
check("деньги без пробела", S("от $5-$10"), "от $5-$10")
check("один доллар", S("цена $5"), "цена $5")
check("экранированный доллар", S(r"это \$5 и \$6"), r"это \$5 и \$6")
check("инлайн-код не трогается", S(r"код `$x^2$` и $x^2$"), r"код `$x^2$` и x²")
check("блок кода не трогается", S("```\n$a \\times b$\n```\n$a \\times b$"), "```\n$a \\times b$\n```\na × b")
check("пустая строка", S(""), "")

h = render_markdown("Будет **6000**. И *курсив*, `код`.\n\n- один\n- два\n\n1. раз\n2. два")
check("markdown: жирный/курсив/код/списки", h, cond=all(t in h for t in ("<strong>6000</strong>", "<em>курсив</em>", "<code>код</code>", "<ul>", "<ol>")))
h = render_markdown("<script>alert(1)</script> <b>x</b> <img src=x onerror=1>")
check("сырой HTML экранируется", h, cond="<script" not in h and "<b>" not in h and "<img" not in h and "&lt;script&gt;" in h)
h = render_markdown("```python\nif a < b:\n    print('<hi>')\n```")
check("код: скобки экранированы, отступы сохранены", h, cond="&lt;hi&gt;" in h and "    print" in h and "<pre>" in h)
h = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
check("таблица с рамкой", h, cond='<table border="1"' in h and "<td>1</td>" in h)
h = render_markdown("```\nнезакрытый блок во время стрима")
check("незакрытый блок кода не падает", h, cond="<pre>" in h)
h = render_markdown("строка 1\nстрока 2")
check("перенос строки сохраняется", h, cond="<br" in h)
print("ALL OK" if ok_all else "SOME FAILED")
