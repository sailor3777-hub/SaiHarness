import json

import os

from core import images
from core.files import estimate_tokens


def format_files(files: list[tuple[str, str]]) -> str:
    """Context files as one block for the system message."""
    blocks = []
    for name, text in files:
        fence = "````" if "```" in text else "```"
        blocks.append(f"=== {name} ===\n{fence}\n{text.rstrip()}\n{fence}")
    return "Context files (content at the time of sending):\n\n" + "\n\n".join(blocks)


def estimate_request_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Token estimate of the whole request: messages (with tool calls) and function descriptions."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):  # text and images: an image counts as a fixed amount, its address is not estimated
            total += estimate_tokens(images.text_of(content)) + images.IMAGE_TOKENS * images.count_images(content) + 4
        else:
            total += estimate_tokens(content or "") + 4  # +4: role service tokens
        if m.get("tool_calls"):
            total += estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False))
    if tools:
        total += estimate_tokens(json.dumps(tools, ensure_ascii=False))
    return total


def _field(row, key: str) -> str:
    """A field of a history row (sqlite3.Row or dict); a missing field is an empty string."""
    try:
        return row[key] or ""
    except (KeyError, IndexError):
        return ""


def history_messages(history) -> list[dict]:
    """Chat history in OpenAI format, including tool calls and their results."""
    out = []
    history = list(history)
    with_images = [i for i, m in enumerate(history) if m["role"] == "user" and images.decode_paths(_field(m, "images"))]
    recent = set(with_images[-images.HISTORY_IMAGE_MESSAGES:])  # old images are not sent again: they are expensive
    for i, m in enumerate(history):
        role, content = m["role"], m["content"]
        if role == "user" and i in with_images:
            paths = images.decode_paths(_field(m, "images"))
            live = [p for p in paths if os.path.exists(p)] if i in recent else []
            gone = [os.path.basename(p) for p in paths if p not in live]
            note = (f"[Images were attached to this message ({', '.join(gone)}); they are not included in this request]"
                    if gone else "")
            text = "\n".join(x for x in (content, note) if x)
            if text or live:
                out.append({"role": "user", "content": images.user_content(text, live)})
        elif role == "user" and content:
            out.append({"role": "user", "content": content})
        elif role == "assistant":
            calls = _field(m, "tool_calls")
            if calls:
                out.append({"role": "assistant", "content": content, "tool_calls": json.loads(calls)})
            elif content:
                out.append({"role": "assistant", "content": content})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": _field(m, "tool_call_id"), "content": content})
    return out


def build_messages(profile, history, files: list[tuple[str, str]] | None = None,
                   catalog_text: str = "") -> list[dict]:
    """Builds the messages for the model from the profile, tool catalog, files and chat history.

    The system prompt, memory, tool catalog and files go in one system message, ordered from
    the most stable to the most changeable so that the server's prompt cache lasts longer. Reasoning
    from earlier replies is not sent back: model templates drop it anyway.
    """
    parts = []
    system = profile["system_prompt"].strip()
    memory = profile["memory"].strip()
    if system:
        parts.append(system)
    if memory:
        parts.append(f"Memory:\n{memory}")
    if catalog_text:
        parts.append(catalog_text)
    if files:
        parts.append(format_files(files))
    messages = [{"role": "system", "content": "\n\n".join(parts)}] if parts else []
    return messages + history_messages(history)
