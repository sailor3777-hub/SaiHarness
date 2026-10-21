"""Rendering model replies: Markdown -> HTML for QTextEdit and simplified LaTeX -> text.

QTextEdit cannot do formulas, so simple formulas are turned into readable Unicode text
(`$58 - 29 = \\mathbf{29}$` -> `58 - 29 = **29**`). Complex constructs are left as they are.
Code (blocks and `inline`) is left alone; raw HTML from the reply is escaped.
"""
import re

from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])

# code fragments that must not be touched when replacing formulas
_CODE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]+`)", re.S)
# $$...$$, \[...\], \(...\), $...$ (pandoc rules: no space after the opening and before the closing delimiter,
# not followed by a digit after the closing one; so "$5 and $10" is not taken for a formula)
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
_ARG = r"\{([^{}]*)\}"  # an argument in curly braces without nested braces


def _latex_to_text(s: str) -> str:
    s = re.sub(r"\\(?:mathbf|textbf|boldsymbol)" + _ARG, r"**\1**", s)
    s = re.sub(r"\\(?:mathit|textit|emph)" + _ARG, r"*\1*", s)
    s = re.sub(r"\\(?:text|mathrm|mathbb|mathcal|operatorname|mbox)" + _ARG, r"\1", s)
    for _ in range(3):  # nested fractions and roots
        s = re.sub(r"\\d?frac" + _ARG + _ARG, lambda m: _frac(m.group(1), m.group(2)), s)
        s = re.sub(r"\\sqrt" + _ARG, r"√(\1)", s)
    s = re.sub(r"\^" + _ARG, lambda m: _script(m.group(1), _SUPER_CHARS, _SUPER, "^"), s)
    s = re.sub(r"\^(\d|n)", lambda m: _script(m.group(1), _SUPER_CHARS, _SUPER, "^"), s)
    s = re.sub(r"_" + _ARG, lambda m: _script(m.group(1), _SUB_CHARS, _SUB, "_"), s)
    s = re.sub(r"_(\d)", lambda m: _script(m.group(1), _SUB_CHARS, _SUB, "_"), s)
    # long commands first: \leftarrow before \le
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
    """Superscript/subscript in Unicode if all characters are in the table, otherwise ^(...) / _(...)."""
    if body and all(ch in chars for ch in body):
        return body.translate(table)
    return f"{fallback}({body})"


def simplify_latex(text: str) -> str:
    """Replaces the formulas $...$, $$...$$, \\(...\\), \\[...\\] with readable text; leaves code alone."""
    parts = _CODE.split(text)
    for i in range(0, len(parts), 2):  # even elements are not code
        parts[i] = _MATH.sub(
            lambda m: _latex_to_text(next(g for g in m.groups() if g is not None)), parts[i]
        )
    return "".join(parts)


def render_markdown(text: str) -> str:
    """Model reply text -> HTML fragment for QTextEdit."""
    html = _md.render(simplify_latex(text))
    return html.replace("<table>", '<table border="1" cellspacing="0" cellpadding="5">')


# styles for QTextDocument.setDefaultStyleSheet: Qt understands only simple tag selectors
DEFAULT_STYLESHEET = """
pre { background-color: #141414; color: #e6e6e6; font-family: Consolas, monospace; }
code { background-color: #141414; color: #f0c674; font-family: Consolas, monospace; }
a { color: #8ab4f8; }
th { background-color: #1c1c1c; }
blockquote { color: #aaaaaa; }
"""
