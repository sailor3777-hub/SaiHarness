"""Lazy tool loading: the model sees three meta-tools and a compact catalog.

Schemas of the real tools stay in the harness and are given to the model on request (describe_tool) as a
call result, that is, at the end of the dialog: the start of the request with the tools list does not change,
and the server's prompt cache is not invalidated.
"""
import difflib
import json
import re
from typing import Callable

import jsonschema

from core.files import estimate_tokens
from core.markers import ERROR_PREFIX, ERROR_WORD
from core.mcp_manager import ToolInfo

MAX_RESULT_CHARS = 12_000
REPEAT_ERROR_BLOCK = 3   # after this many identical errors in a row the tool is no longer called in this reply
REPEAT_ERROR_KEY = 160   # "the same" error is recognized by the start of its text
TOOL_TIMEOUTS = {"toolbox.vision_describe": 660.0, "toolbox.shell_run": 90.0, "toolbox.image_generate": 960.0}  # longer than the general call limit: the vision model and ComfyUI take minutes, shell_run waits up to shell.WAIT (45 s) before leaving the command in the background, plus a margin
CATALOG_MAX_LISTED = 40   # with more tools the catalog is not listed, search only
DESCRIPTION_MAX = 110     # description length in a catalog line
FIND_LIMIT = 6

META_TOOLS = [
    {"type": "function", "function": {
        "name": "find_tool", "description": "Find a tool by describing the task.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "describe_tool", "description": "Full argument schema of a catalog tool.",
        "parameters": {"type": "object", "properties": {"tool": {"type": "string"}}, "required": ["tool"]}}},
    {"type": "function", "function": {
        "name": "call_tool", "description": "Call a tool from the catalog.",
        # the parameter is called tool, not name: many tools have their own name argument, and models mix them up
        "parameters": {"type": "object", "properties": {
            "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["tool", "arguments"]}}},
]
META_NAMES = {t["function"]["name"] for t in META_TOOLS}


def _tool_timeout(tool) -> float:
    """The call budget of a tool; a host_<name>.<tool> of a remote agent gets the budget of the local tool of the same name."""
    own = TOOL_TIMEOUTS.get(tool.id)
    if own is None and tool.server.startswith("host_"):
        own = TOOL_TIMEOUTS.get(f"toolbox.{tool.name}")
    return own or 0.0


# Tool dependencies: enabling a key pulls in its values (and their dependencies). Empty for now: memory needs no
# other tools; an entry will appear together with file indexing, which needs fs_read.
REQUIRES: dict[str, tuple[str, ...]] = {}


def with_requirements(tool_ids, requires: dict[str, tuple[str, ...]] | None = None) -> set[str]:
    """The set of tools together with what they require (transitively; cycles are fine)."""
    requires = REQUIRES if requires is None else requires
    result, queue = set(), list(tool_ids)
    while queue:
        tool_id = queue.pop()
        if tool_id not in result:
            result.add(tool_id)
            queue.extend(requires.get(tool_id, ()))
    return result


def default_policy(tool: ToolInfo) -> str:
    """auto | ask | deny. Reading runs on its own; everything else (and anything unknown) asks."""
    return "auto" if tool.read_only else "ask"


_TYPE_NAMES = {"string": "str", "integer": "int", "number": "num", "boolean": "bool", "array": "list", "object": "obj"}


def signature(tool: ToolInfo) -> str:
    """Name(arguments with types): the model sees what to pass and needs describe_tool less often."""
    props = (tool.input_schema or {}).get("properties") or {}
    required = set((tool.input_schema or {}).get("required") or [])
    parts = []
    for name, spec in props.items():
        type_name = _TYPE_NAMES.get(spec.get("type")) if isinstance(spec, dict) else None
        parts.append(f"{name}{'' if name in required else '?'}{f': {type_name}' if type_name else ''}")
    return f"{tool.id}({', '.join(parts)})"


def _strip_titles(value):
    """Removes the auxiliary "title" keys from a schema: they repeat the field name and waste tokens."""
    if isinstance(value, dict):
        return {k: _strip_titles(v) for k, v in value.items() if k != "title"}
    if isinstance(value, list):
        return [_strip_titles(v) for v in value]
    return value


def _short(text: str, limit: int) -> str:
    first = re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _same_word(a: str, b: str) -> bool:
    """Words match if they are equal or share a common part of 4+ characters covering 60% of the shorter word.

    So "calculate" finds "calculates", "reading" finds "read", "files" finds "file",
    while "computer" and "commuter" (common tail "uter") do not stick together.
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
    """Tool name from the meta-function arguments: the main parameter is tool, the fallback is name (a model habit)."""
    return args.get("tool") if args.get("tool") else args.get("name")


def describe_call(name: str, arguments: str) -> str:
    """A short line about the call for the chat: what exactly the model asked for."""
    try:
        args = json.loads(arguments) if arguments and arguments.strip() else {}
    except ValueError:
        return f"{name}({arguments[:120]})"
    if not isinstance(args, dict):
        return f"{name}({_compact(args)})"
    if name == "find_tool":
        return f"tool search: \"{args.get('query', '')}\""
    if name == "describe_tool":
        return f"tool schema {_tool_arg(args) or ''}"
    if name == "call_tool":
        inner = args.get("arguments")
        return f"{_tool_arg(args) or '?'}({_compact(inner) if inner not in (None, {}) else ''})"
    return f"{name}({_compact(args) if args else ''})"


def result_label(name: str, arguments: str) -> str:
    """Result label: the name of the real tool (for call_tool) or of the meta-function."""
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
    """How much the full schemas would take if every tool were passed in tools (for comparison)."""
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
        """Block for the system message (empty if there are no tools)."""
        if not self.tools:
            return ""
        head = ("Tools. Call them only through call_tool(tool, arguments): tool is the full name from the catalog, "
                "arguments is an object with the arguments. The catalog signature is enough to make a call; use "
                "describe_tool only if it is unclear what to pass. If the tool you need is not in the catalog, use "
                "find_tool(query), and if it really does not exist, say so. If a call returns "
                f"{ERROR_WORD}, do not make up a result: fix the call or report the error. Do not promise to do "
                "something, call the tool.")
        if len(self.tools) > CATALOG_MAX_LISTED:
            by_server: dict[str, int] = {}
            for t in self.tools:
                by_server[t.server] = by_server.get(t.server, 0) + 1
            servers = ", ".join(f"{s} ({n})" for s, n in by_server.items())
            return f"{head}\nThere are {len(self.tools)} tools; the catalog is not listed, search with find_tool. Servers: {servers}."
        lines = "\n".join(f"- {self.line(t)}" for t in self.tools)
        return f"{head}\nCatalog (an argument marked \"?\" is optional):\n{lines}"

    def describe(self, tool: ToolInfo) -> str:
        """Description and argument schema as compact JSON (without the auxiliary "title" keys)."""
        return json.dumps({
            "name": tool.id, "description": tool.description,
            "arguments_schema": _strip_titles(tool.input_schema),
            "read_only": bool(tool.read_only),
        }, ensure_ascii=False)

    def find(self, query: str, limit: int = FIND_LIMIT) -> list[ToolInfo]:
        """Search by name (weight 3) and description (weight 1); words are compared allowing for word forms."""
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
    """Argument errors against the tool's JSON schema (an empty list means everything is fine)."""
    if not isinstance(arguments, dict):
        return ["arguments must be a JSON object"]
    schema = tool.input_schema or {}
    try:
        validator = jsonschema.validators.validator_for(schema)(schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(map(str, e.absolute_path)))
    except jsonschema.SchemaError:
        return []  # the server's schema is invalid: let the server decide
    return [
        f"{'/'.join(map(str, e.absolute_path)) or 'arguments'}: {e.message}" for e in errors[:5]
    ]


class ToolRouter:
    """Runs meta-tool calls. Always returns text: errors are text too, so the model can correct itself."""

    def __init__(self, manager, catalog: Catalog, policy_for: Callable[[ToolInfo], str] = default_policy,
                 confirm: Callable[[str, dict], bool] | None = None, call_timeout: float = 60.0):
        self.manager, self.catalog, self.policy_for = manager, catalog, policy_for
        self.confirm, self.call_timeout = confirm, call_timeout
        self.stuck: str | None = None  # tool whose call was blocked because of a repeating error: the agent loop stops on it
        self._errors: dict[str, tuple[str, int]] = {}  # tool -> (start of the last error text, how many times in a row)

    # --- entry ---

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
        if self.catalog.get(name):  # the model called a tool directly: run it, that is clearly what it meant
            return self._call(name, args)
        return ERROR_PREFIX + f"there is no function \"{name}\". Available: find_tool, describe_tool, call_tool."

    @staticmethod
    def _parse(arguments) -> dict | str:
        if isinstance(arguments, dict):
            return arguments
        if arguments is None or not str(arguments).strip():
            return {}
        try:
            parsed = json.loads(arguments)
        except ValueError as e:
            return ERROR_PREFIX + f"the arguments are not valid JSON ({e})."
        return parsed if isinstance(parsed, dict) else ERROR_PREFIX + "the arguments must be a JSON object."

    # --- meta-tools ---

    def _find(self, query) -> str:
        if not isinstance(query, str) or not query.strip():
            return ERROR_PREFIX + "pass query: what needs to be done."
        found = self.catalog.find(query)
        if not found:
            names = ", ".join(t.id for t in self.catalog.tools[:20])
            return f"No matching tools found. All tools: {names or 'none'}."
        return "\n".join(self.catalog.line(t) for t in found)

    def _describe(self, name) -> str:
        tool = self.catalog.get(name) if isinstance(name, str) else None
        if tool is None:
            return self._unknown(name)
        return self.catalog.describe(tool)

    def _unknown(self, name) -> str:
        if not isinstance(name, str) or not name.strip():
            return (ERROR_PREFIX + 'no tool name given. Format: call_tool(tool="server.tool", '
                    'arguments={...}); take the names from the catalog.')
        hint = [t.id for t in self.catalog.find(name)[:3]] or [t.id for t in self.catalog.tools[:5]]
        return ERROR_PREFIX + f"there is no tool \"{name}\" in the catalog. Did you mean: {', '.join(hint) or 'the catalog is empty'}."

    def _call(self, name, arguments) -> str:
        tool = self.catalog.get(name) if isinstance(name, str) else None
        if tool is None:
            return self._unknown(name)
        if isinstance(arguments, str):  # models often send the object as a string
            parsed = self._parse(arguments)
            if isinstance(parsed, str):
                return parsed
            arguments = parsed
        arguments = {} if arguments is None else arguments
        problems = validate_arguments(tool, arguments)
        if problems:
            return (ERROR_PREFIX + "invalid arguments for " + tool.id + ": " + "; ".join(problems)
                    + "\nArgument schema:\n" + json.dumps(tool.input_schema, ensure_ascii=False))
        policy = self.policy_for(tool)
        if policy == "deny":
            return ERROR_PREFIX + f"calling {tool.id} is blocked in the user's settings."
        if policy == "ask" and not (self.confirm and self.confirm(tool.id, arguments)):
            return f"The user declined the call to {tool.id}. Do not repeat it unless asked again."
        last = self._errors.get(tool.id)
        if last and last[1] >= REPEAT_ERROR_BLOCK - 1:
            self.stuck = tool.id
            return (ERROR_PREFIX + f"{tool.id} was not called: it has already returned the same error {last[1]} times "
                    f"in a row ({last[0]}). Do not call it again: tell the user what went wrong and why, "
                    "and carry on without this tool.")
        text, is_error = self.manager.call(tool.server, tool.name, arguments, max(self.call_timeout, _tool_timeout(tool)))
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n[truncated: showing {MAX_RESULT_CHARS} of {len(text)} characters]"
        if not is_error:
            self._errors.pop(tool.id, None)
            return text or "(empty result)"
        key = text[:REPEAT_ERROR_KEY]
        count = last[1] + 1 if last and last[0] == key else 1
        self._errors[tool.id] = (key, count)
        hint = ""
        if count >= 2:
            hint = (f"\nThis is the same {tool.id} error {count} times in a row. Calling it again is pointless: "
                    "tell the user the tool is not working and carry on without it.")
        return ERROR_PREFIX + f"{text}{hint}"
