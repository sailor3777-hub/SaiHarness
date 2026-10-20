"""Отрисовка ответов модели: Markdown -> HTML для QTextEdit и упрощённый LaTeX -> текст.

QTextEdit не умеет формулы, поэтому простые формулы превращаются в читаемый текст с юникодом
(`$58 - 29 = \\mathbf{29}$` -> `58 - 29 = **29**`). Сложные конструкции остаются как есть.
Код (блоки и `inline`) не трогается, сырой HTML из ответа экранируется.
"""
import re

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])

# фрагменты кода, которые не надо трогать при замене формул
_CODE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.S)
# $$...$$, \[...\], \(...\), $...$ (правила pandoc: после открывающего и перед закрывающим не пробел,
# после закрывающего не цифра; так «$5 и $10» не принимается за формулу)
_MATH = re.compile(
    r"\$\$(?P<d1>.+?)\$\$|\\\[(?P<d2>.+?)\\\]|\\\((?P<i1>.+?)\\\)"
    r"|(?<![\\$])\$(?![\s$])(?P<i2>[^$\n]+?)(?<![\s\\])\$(?!\d)",
    re.S,
)

_SYMBOLS = {
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±", r"\mp": "∓", r"\le": "≤", r"\leq": "≤",
    r"\ge": "≥", r"\geq": "≥", r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\equiv": "≡", r"\infty": "∞",
    r"\to": "→", r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\leftarrow": "←", r"\leftrightarrow": "↔",
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫", r"\partial": "∂", r"\nabla": "∇", r"\in": "∈",
    r"\subset": "⊂", r"\cup": "∪", r"\cap": "∩", r"\ldots": "…", r"\dots": "…", r"\circ": "∘",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\epsilon": "ε", r"\theta": "θ",
    r"\lambda": "λ", r"\mu": "μ", r"\pi": "π", r"\sigma": "σ", r"\phi": "φ", r"\omega": "ω",
    r"\Delta": "Δ", r"\Sigma": "Σ", r"\Omega": "Ω", r"\%": "%", r"\$": "$", r"\&": "&", r"\_": "_",
}
_SUPER_CHARS, _SUB_CHARS = "0123456789+-n", "0123456789+-"
_SUPER = str.maketrans(_SUPER_CHARS, "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ")
_SUB = str.maketrans(_SUB_CHARS, "₀₁₂₃₄₅₆₇₈₉₊₋")
_ARG = r"\{([^{}]*)\}"  # аргумент в фигурных скобках без вложенных скобок


def _latex_to_text(s: str) -> str:
    s = re.sub(r"\\(?:mathbf|textbf|boldsymbol)" + _ARG, r"**\1**", s)
    s = re.sub(r"\\(?:mathit|textit|emph)" + _ARG, r"*\1*", s)
    s = re.sub(r"\\(?:text|mathrm|mathbb|mathcal|operatorname|mbox)" + _ARG, r"\1", s)
    for _ in range(3):  # вложенные дроби и корни
        s = re.sub(r"\\d?frac" + _ARG + _ARG, lambda m: _frac(m.group(1), m.group(2)), s)
        s = re.sub(r"\\sqrt" + _ARG, r"√(\1)", s)
    s = re.sub(r"\^" + _ARG, lambda m: _script(m.group(1), _SUPER_CHARS, _SUPER, "^"), s)
    s = re.sub(r"\^(\d|n)", lambda m: _script(m.group(1), _SUPER_CHARS, _SUPER, "^"), s)
    s = re.sub(r"_" + _ARG, lambda m: _script(m.group(1), _SUB_CHARS, _SUB, "_"), s)
    s = re.sub(r"_(\d)", lambda m: _script(m.group(1), _SUB_CHARS, _SUB, "_"), s)
    # длинные команды первыми: \leftarrow раньше \le
    for cmd in sorted(_SYMBOLS, key=len, reverse=True):
        s = re.sub(re.escape(cmd) + r"(?![A-Za-z])", lambda _m, v=_SYMBOLS[cmd]: v, s)
    s = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)(?![A-Za-z])", "", s)
    s = re.sub(r"\\[,;:! ]", " ", s)
    s = re.sub(r"\\quad|\\qquad", "  ", s)
    return s.replace("{", "").replace("}", "").strip()


def _frac(a: str, b: str) -> str:
    wrap = lambda x: x if re.fullmatch(r"[\w.]+", x) else f"({x})"
    return f"{wrap(a)}/{wrap(b)}"


def _script(body: str, chars: str, table: dict, fallback: str) -> str:
    """Верхний/нижний индекс юникодом, если все символы есть в таблице, иначе ^(...) / _(...)."""
    if body and all(ch in chars for ch in body):
        return body.translate(table)
    return f"{fallback}({body})"


def simplify_latex(text: str) -> str:
    """Заменяет формулы $...$, $$...$$, \\(...\\), \\[...\\] читаемым текстом; код не трогает."""
    parts = _CODE.split(text)
    for i in range(0, len(parts), 2):  # чётные элементы — не код
        parts[i] = _MATH.sub(
            lambda m: _latex_to_text(next(g for g in m.groups() if g is not None)), parts[i]
        )
    return "".join(parts)


def render_markdown(text: str) -> str:
    """Текст ответа модели -> HTML-фрагмент для QTextEdit."""
    html = _md.render(simplify_latex(text))
    return html.replace("<table>", '<table border="1" cellspacing="0" cellpadding="5">')


# стили для QTextDocument.setDefaultStyleSheet: Qt понимает только простые селекторы по тегам
DEFAULT_STYLESHEET = """
pre { background-color: #141414; color: #e6e6e6; font-family: Consolas, monospace; }
code { background-color: #141414; color: #f0c674; font-family: Consolas, monospace; }
a { color: #8ab4f8; }
th { background-color: #1c1c1c; }
blockquote { color: #aaaaaa; }
"""
