"""Ленивая загрузка инструментов: модель видит три мета-инструмента и компактный каталог.

Схемы реальных инструментов остаются в обвязке и отдаются модели по запросу (describe_tool) как
результат вызова, то есть в конец диалога: начало запроса со списком tools не меняется, и кеш
промпта сервера не сбрасывается.
"""
import difflib
import json
import re
from typing import Callable

import jsonschema

from core.files import estimate_tokens
from core.mcp_manager import ToolInfo

MAX_RESULT_CHARS = 12_000
REPEAT_ERROR_BLOCK = 3   # на столько одинаковых ошибок подряд инструмент перестаёт вызываться в этом ответе
REPEAT_ERROR_KEY = 160   # по началу текста ошибки узнаём «ту же самую» ошибку
TOOL_TIMEOUTS = {"toolbox.vision_describe": 660.0, "toolbox.shell_run": 90.0, "toolbox.image_generate": 960.0}  # дольше общего лимита вызова: vision-модель и ComfyUI отвечают минутами, у shell_run свои 60 с плюс запас
CATALOG_MAX_LISTED = 40   # больше инструментов: каталог не перечисляется, только поиск
DESCRIPTION_MAX = 110     # длина описания в строке каталога
FIND_LIMIT = 6

META_TOOLS = [
    {"type": "function", "function": {
        "name": "find_tool", "description": "Найти инструмент по описанию задачи.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "describe_tool", "description": "Полная схема аргументов инструмента из каталога.",
        "parameters": {"type": "object", "properties": {"tool": {"type": "string"}}, "required": ["tool"]}}},
    {"type": "function", "function": {
        "name": "call_tool", "description": "Вызвать инструмент из каталога.",
        # параметр называется tool, а не name: у многих инструментов есть свой аргумент name, и модели их путают
        "parameters": {"type": "object", "properties": {
            "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["tool", "arguments"]}}},
]
META_NAMES = {t["function"]["name"] for t in META_TOOLS}


# Зависимости инструментов: включение ключа тянет за собой значения (и их зависимости). Пока пусто: память сама никаких
# других инструментов не требует; запись сюда появится вместе с индексацией файлов, которой нужен fs_read.
REQUIRES: dict[str, tuple[str, ...]] = {}


def with_requirements(tool_ids, requires: dict[str, tuple[str, ...]] | None = None) -> set[str]:
    """Набор инструментов вместе с тем, что им требуется (транзитивно, циклы не страшны)."""
    requires = REQUIRES if requires is None else requires
    result, queue = set(), list(tool_ids)
    while queue:
        tool_id = queue.pop()
        if tool_id not in result:
            result.add(tool_id)
            queue.extend(requires.get(tool_id, ()))
    return result


def default_policy(tool: ToolInfo) -> str:
    """auto | ask | deny. Чтение выполняется само, всё остальное (и неизвестное) спрашивает."""
    return "auto" if tool.read_only else "ask"


_TYPE_NAMES = {"string": "str", "integer": "int", "number": "num", "boolean": "bool", "array": "list", "object": "obj"}


def signature(tool: ToolInfo) -> str:
    """Имя(аргументы с типами): так модель видит, что передавать, и ей реже нужен describe_tool."""
    props = (tool.input_schema or {}).get("properties") or {}
    required = set((tool.input_schema or {}).get("required") or [])
    parts = []
    for name, spec in props.items():
        type_name = _TYPE_NAMES.get(spec.get("type")) if isinstance(spec, dict) else None
        parts.append(f"{name}{'' if name in required else '?'}{f': {type_name}' if type_name else ''}")
    return f"{tool.id}({', '.join(parts)})"


def _strip_titles(value):
    """Убирает служебные «title» из схемы: они дублируют имя поля и тратят токены."""
    if isinstance(value, dict):
        return {k: _strip_titles(v) for k, v in value.items() if k != "title"}
    if isinstance(value, list):
        return [_strip_titles(v) for v in value]
    return value


def _short(text: str, limit: int) -> str:
    first = re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _same_word(a: str, b: str) -> bool:
    """Слова совпадают, если равны или делят общую часть от 4 символов, покрывающую 60% короткого слова.

    Так «вычислить» находит «вычисляет», «прочитать» находит «читает», «файлы» находит «файл»,
    а «использовать» и «читать» (общий хвост «ать») не склеиваются.
    """
    if a == b:
        return True
    shorter = min(len(a), len(b))
    if shorter < 4:
        return False
    size = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b)).size
    return size >= 4 and size >= 0.6 * shorter


def _compact(value, limit: int = 200) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_arg(args: dict):
    """Имя инструмента из аргументов мета-функции: основной параметр tool, запасной name (привычка моделей)."""
    return args.get("tool") if args.get("tool") else args.get("name")


def describe_call(name: str, arguments: str) -> str:
    """Короткая строка о вызове для чата: что именно модель попросила сделать."""
    try:
        args = json.loads(arguments) if arguments and arguments.strip() else {}
    except ValueError:
        return f"{name}({arguments[:120]})"
    if not isinstance(args, dict):
        return f"{name}({_compact(args)})"
    if name == "find_tool":
        return f"поиск инструмента: «{args.get('query', '')}»"
    if name == "describe_tool":
        return f"схема инструмента {_tool_arg(args) or ''}"
    if name == "call_tool":
        inner = args.get("arguments")
        return f"{_tool_arg(args) or '?'}({_compact(inner) if inner not in (None, {}) else ''})"
    return f"{name}({_compact(args) if args else ''})"


def result_label(name: str, arguments: str) -> str:
    """Подпись результата: имя настоящего инструмента (для call_tool) или мета-функции."""
    if name == "call_tool":
        try:
            inner = _tool_arg(json.loads(arguments))
            if isinstance(inner, str) and inner:
                return inner
        except (ValueError, AttributeError):
            pass
    return name


def meta_tools_tokens() -> int:
    return estimate_tokens(json.dumps(META_TOOLS, ensure_ascii=False))


def schema_tokens(tools: list[ToolInfo]) -> int:
    """Сколько заняли бы полные схемы, если бы все инструменты передавались в tools (для сравнения)."""
    total = 0
    for t in tools:
        spec = {"type": "function", "function": {
            "name": t.id.replace(".", "__"), "description": t.description, "parameters": t.input_schema}}
        total += estimate_tokens(json.dumps(spec, ensure_ascii=False))
    return total


class Catalog:
    def __init__(self, tools: list[ToolInfo]):
        self.tools = list(tools)
        self._by_id = {t.id: t for t in self.tools}

    def get(self, name: str) -> ToolInfo | None:
        name = (name or "").strip()
        if name in self._by_id:
            return self._by_id[name]
        lowered = [t for t in self.tools if t.id.lower() == name.lower()]
        if lowered:
            return lowered[0]
        bare = [t for t in self.tools if t.name.lower() == name.lower()]
        return bare[0] if len(bare) == 1 else None

    def line(self, tool: ToolInfo) -> str:
        desc = _short(tool.description, DESCRIPTION_MAX)
        return f"{signature(tool)} — {desc}" if desc else signature(tool)

    def text(self) -> str:
        """Блок для system-сообщения (пусто, если инструментов нет)."""
        if not self.tools:
            return ""
        head = ("Инструменты. Вызывай их только через call_tool(tool, arguments): tool — полное имя из каталога, "
                "arguments — объект с аргументами. Сигнатуры из каталога достаточно для вызова; describe_tool "
                "нужен только если непонятно, что передавать. Нужного инструмента нет в каталоге — find_tool(query), "
                "а если его правда нет, так и скажи. Если вызов вернул ОШИБКА, не выдумывай результат: исправь "
                "вызов или сообщи об ошибке. Не обещай выполнить действие, а вызывай инструмент.")
        if len(self.tools) > CATALOG_MAX_LISTED:
            by_server: dict[str, int] = {}
            for t in self.tools:
                by_server[t.server] = by_server.get(t.server, 0) + 1
            servers = ", ".join(f"{s} ({n})" for s, n in by_server.items())
            return f"{head}\nИнструментов {len(self.tools)}, каталог не перечисляется, ищи через find_tool. Серверы: {servers}."
        lines = "\n".join(f"- {self.line(t)}" for t in self.tools)
        return f"{head}\nКаталог (аргумент со знаком «?» необязателен):\n{lines}"

    def describe(self, tool: ToolInfo) -> str:
        """Описание и схема аргументов компактным JSON (без служебных «title»)."""
        return json.dumps({
            "name": tool.id, "description": tool.description,
            "arguments_schema": _strip_titles(tool.input_schema),
            "read_only": bool(tool.read_only),
        }, ensure_ascii=False)

    def find(self, query: str, limit: int = FIND_LIMIT) -> list[ToolInfo]:
        """Поиск по имени (вес 3) и описанию (вес 1); слова сравниваются с учётом русских словоформ."""
        words = [w for w in re.findall(r"\w+", (query or "").lower().replace("_", " ")) if len(w) > 1]
        scored = []
        for t in self.tools:
            name_words = re.findall(r"\w+", t.id.lower().replace("_", " "))
            desc_words = re.findall(r"\w+", t.description.lower())
            score = sum(
                3 * any(_same_word(w, x) for x in name_words) + any(_same_word(w, x) for x in desc_words)
                for w in words
            )
            if score:
                scored.append((-score, t.id, t))
        return [t for _, _, t in sorted(scored)[:limit]]


def validate_arguments(tool: ToolInfo, arguments) -> list[str]:
    """Ошибки аргументов по JSON-схеме инструмента (пустой список = всё верно)."""
    if not isinstance(arguments, dict):
        return ["аргументы должны быть объектом JSON"]
    schema = tool.input_schema or {}
    try:
        validator = jsonschema.validators.validator_for(schema)(schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(map(str, e.absolute_path)))
    except jsonschema.SchemaError:
        return []  # схема сервера некорректна: пусть решает сам сервер
    return [
        f"{'/'.join(map(str, e.absolute_path)) or 'аргументы'}: {e.message}" for e in errors[:5]
    ]


class ToolRouter:
    """Исполняет вызовы мета-инструментов. Всегда возвращает текст: ошибки тоже текст, чтобы модель могла исправиться."""

    def __init__(self, manager, catalog: Catalog, policy_for: Callable[[ToolInfo], str] = default_policy,
                 confirm: Callable[[str, dict], bool] | None = None, call_timeout: float = 60.0):
        self.manager, self.catalog, self.policy_for = manager, catalog, policy_for
        self.confirm, self.call_timeout = confirm, call_timeout
        self.stuck: str | None = None  # инструмент, вызов которого заблокирован из-за повторяющейся ошибки: цикл агента на этом останавливается
        self._errors: dict[str, tuple[str, int]] = {}  # инструмент -> (начало текста последней ошибки, сколько раз подряд)

    # --- вход ---

    def handle(self, name: str, arguments: str | dict | None) -> str:
        args = self._parse(arguments)
        if isinstance(args, str):
            return args
        if name == "find_tool":
            return self._find(args.get("query"))
        if name == "describe_tool":
            return self._describe(_tool_arg(args))
        if name == "call_tool":
            return self._call(_tool_arg(args), args.get("arguments"))
        if self.catalog.get(name):  # модель позвала инструмент напрямую: выполняем, она явно имела в виду его
            return self._call(name, args)
        return f"ОШИБКА: функции «{name}» нет. Доступны: find_tool, describe_tool, call_tool."

    @staticmethod
    def _parse(arguments) -> dict | str:
        if isinstance(arguments, dict):
            return arguments
        if arguments is None or not str(arguments).strip():
            return {}
        try:
            parsed = json.loads(arguments)
        except ValueError as e:
            return f"ОШИБКА: аргументы не являются корректным JSON ({e})."
        return parsed if isinstance(parsed, dict) else "ОШИБКА: аргументы должны быть объектом JSON."

    # --- мета-инструменты ---

    def _find(self, query) -> str:
        if not isinstance(query, str) or not query.strip():
            return "ОШИБКА: укажи query — что нужно сделать."
        found = self.catalog.find(query)
        if not found:
            names = ", ".join(t.id for t in self.catalog.tools[:20])
            return f"Подходящих инструментов не нашлось. Все инструменты: {names or 'нет'}."
        return "\n".join(self.catalog.line(t) for t in found)

    def _describe(self, name) -> str:
        tool = self.catalog.get(name) if isinstance(name, str) else None
        if tool is None:
            return self._unknown(name)
        return self.catalog.describe(tool)

    def _unknown(self, name) -> str:
        if not isinstance(name, str) or not name.strip():
            return ('ОШИБКА: не указано имя инструмента. Формат: call_tool(tool="сервер.инструмент", '
                    'arguments={...}); имена бери из каталога.')
        hint = [t.id for t in self.catalog.find(name)[:3]] or [t.id for t in self.catalog.tools[:5]]
        return f"ОШИБКА: инструмента «{name}» нет в каталоге. Возможно: {', '.join(hint) or 'каталог пуст'}."

    def _call(self, name, arguments) -> str:
        tool = self.catalog.get(name) if isinstance(name, str) else None
        if tool is None:
            return self._unknown(name)
        if isinstance(arguments, str):  # модели часто присылают объект строкой
            parsed = self._parse(arguments)
            if isinstance(parsed, str):
                return parsed
            arguments = parsed
        arguments = {} if arguments is None else arguments
        problems = validate_arguments(tool, arguments)
        if problems:
            return ("ОШИБКА: неверные аргументы для " + tool.id + ": " + "; ".join(problems)
                    + "\nСхема аргументов:\n" + json.dumps(tool.input_schema, ensure_ascii=False))
        policy = self.policy_for(tool)
        if policy == "deny":
            return f"ОШИБКА: вызов {tool.id} запрещён настройками пользователя."
        if policy == "ask" and not (self.confirm and self.confirm(tool.id, arguments)):
            return f"Пользователь отклонил вызов {tool.id}. Не повторяй его без новой просьбы."
        last = self._errors.get(tool.id)
        if last and last[1] >= REPEAT_ERROR_BLOCK - 1:
            self.stuck = tool.id
            return (f"ОШИБКА: вызов {tool.id} не выполнен: он уже {last[1]} раза подряд вернул одну и ту же ошибку "
                    f"({last[0]}). Не вызывай его снова: объясни пользователю, что не получилось и почему, "
                    "и продолжай без этого инструмента.")
        text, is_error = self.manager.call(tool.server, tool.name, arguments, max(self.call_timeout, TOOL_TIMEOUTS.get(tool.id, 0.0)))
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n[обрезано: показано {MAX_RESULT_CHARS} из {len(text)} символов]"
        if not is_error:
            self._errors.pop(tool.id, None)
            return text or "(пустой результат)"
        key = text[:REPEAT_ERROR_KEY]
        count = last[1] + 1 if last and last[0] == key else 1
        self._errors[tool.id] = (key, count)
        hint = ""
        if count >= 2:
            hint = (f"\nЭто уже {count}-я подряд такая же ошибка {tool.id}. Повторять вызов бессмысленно: "
                    "сообщи пользователю, что инструмент не работает, и продолжай без него.")
        return f"ОШИБКА: {text}{hint}"
