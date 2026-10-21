from core.richtext import simplify_latex, render_markdown

ok_all = True
def check(name, got, want=None, cond=None):
    global ok_all
    good = cond if cond is not None else got == want
    ok_all &= bool(good)
    print(("OK   " if good else "FAIL ") + name + ("" if good else f"\n       got : {got!r}\n       want: {want!r}"))

S = simplify_latex
check("user example", S(r"$58 - 29 = \mathbf{29}$"), "58 - 29 = **29**")
check("fraction", S(r"$\frac{1}{2}$"), "1/2")
check("a fraction with a complex numerator", S(r"$\frac{a+b}{c}$"), "(a+b)/c")
check("exponent", S(r"$x^2 + y^{10}$"), "x² + y¹⁰")
check("exponent with a letter -> fallback", S(r"$e^{-x}$"), "e^(-x)")
check("subscript", S(r"$a_1 + a_{12}$"), "a₁ + a₁₂")
check("root", S(r"$\sqrt{16} = 4$"), "√(16) = 4")
check("symbols", S(r"$3 \times 4 \cdot 2 \div 1 \neq \pi$"), "3 × 4 · 2 ÷ 1 ≠ π")
check("\\le does not break \\left", S(r"$\left( x \le 5 \right)$"), "( x ≤ 5 )")
check("\\leftarrow", S(r"$a \leftarrow b$"), "a ← b")
check("\\in and \\int", S(r"$x \in A, \int f$"), "x ∈ A, ∫ f")
check("\\( \\) and \\[ \\]", S(r"\(a \times b\) and \[c \pm d\]"), "a × b and c ± d")
check("$$ block $$", S("$$\n\\frac{a}{b}\n$$"), "a/b")
check("\\text inside", S(r"$5 \text{ apples}$"), "5  apples")
check("money is not a formula", S("Costs $5 and $10 in total"), "Costs $5 and $10 in total")
check("money without a space", S("from $5-$10"), "from $5-$10")
check("one dollar", S("price $5"), "price $5")
check("escaped dollar", S(r"this is \$5 and \$6"), r"this is \$5 and \$6")
check("inline code is not touched", S(r"code `$x^2$` and $x^2$"), r"code `$x^2$` and x²")
check("a code block is not touched", S("```\n$a \\times b$\n```\n$a \\times b$"), "```\n$a \\times b$\n```\na × b")
check("empty string", S(""), "")

h = render_markdown("It is **6000**. And *italic*, `code`.\n\n- one\n- two\n\n1. one\n2. two")
check("markdown: bold/italic/code/lists", h, cond=all(t in h for t in ("<strong>6000</strong>", "<em>italic</em>", "<code>code</code>", "<ul>", "<ol>")))
h = render_markdown("<script>alert(1)</script> <b>x</b> <img src=x onerror=1>")
check("raw HTML is escaped", h, cond="<script" not in h and "<b>" not in h and "<img" not in h and "&lt;script&gt;" in h)
h = render_markdown("```python\nif a < b:\n    print('<hi>')\n```")
check("code: brackets escaped, indentation kept", h, cond="&lt;hi&gt;" in h and "    print" in h and "<pre>" in h)
h = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
check("a table with a border", h, cond='<table border="1"' in h and "<td>1</td>" in h)
h = render_markdown("```\nan unclosed block while streaming")
check("an unclosed code block does not crash", h, cond="<pre>" in h)
h = render_markdown("line 1\nline 2")
check("the line break is kept", h, cond="<br" in h)
print("ALL OK" if ok_all else "SOME FAILED")
