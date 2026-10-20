import json

import os

from core import images
from core.files import estimate_tokens


def format_files(files: list[tuple[str, str]]) -> str:
    """Файлы контекста одним блоком для system-сообщения."""
    blocks = []
    for name, text in files:
        fence = "````" if "```" in text else "```"
        blocks.append(f"=== {name} ===\n{fence}\n{text.rstrip()}\n{fence}")
    return "Файлы контекста (содержимое на момент отправки):\n\n" + "\n\n".join(blocks)


def estimate_request_tokens(messages: list[dict], tools: list[dict] | None = None) -> int:
    """Оценка токенов всего запроса: сообщения (с вызовами инструментов) и описания функций."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):  # текст и картинки: картинка считается фиксированно, её адрес в оценку не входит
            total += estimate_tokens(images.text_of(content)) + images.IMAGE_TOKENS * images.count_images(content) + 4
        else:
            total += estimate_tokens(content or "") + 4  # +4: служебные токены ролей
        if m.get("tool_calls"):
            total += estimate_tokens(json.dumps(m["tool_calls"], ensure_ascii=False))
    if tools:
        total += estimate_tokens(json.dumps(tools, ensure_ascii=False))
    return total


def _field(row, key: str) -> str:
    """Поле строки истории (sqlite3.Row или словарь); отсутствующее поле = пустая строка."""
    try:
        return row[key] or ""
    except (KeyError, IndexError):
        return ""


def history_messages(history) -> list[dict]:
    """История чата в формате OpenAI, включая вызовы инструментов и их результаты."""
    out = []
    history = list(history)
    with_images = [i for i, m in enumerate(history) if m["role"] == "user" and images.decode_paths(_field(m, "images"))]
    recent = set(with_images[-images.HISTORY_IMAGE_MESSAGES:])  # старые картинки повторно не отправляем: они дорогие
    for i, m in enumerate(history):
        role, content = m["role"], m["content"]
        if role == "user" and i in with_images:
            paths = images.decode_paths(_field(m, "images"))
            live = [p for p in paths if os.path.exists(p)] if i in recent else []
            gone = [os.path.basename(p) for p in paths if p not in live]
            note = (f"[К сообщению были приложены изображения ({', '.join(gone)}), в этот запрос они не включены]"
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
    """Собирает сообщения для модели из профиля, каталога инструментов, файлов и истории чата.

    System prompt, память, каталог инструментов и файлы идут одним system-сообщением; порядок от
    самого стабильного к самому изменчивому, чтобы кеш промпта сервера работал дольше. Размышления
    прежних ответов в запрос не возвращаются: шаблоны моделей их всё равно отбрасывают.
    """
    parts = []
    system = profile["system_prompt"].strip()
    memory = profile["memory"].strip()
    if system:
        parts.append(system)
    if memory:
        parts.append(f"Память:\n{memory}")
    if catalog_text:
        parts.append(catalog_text)
    if files:
        parts.append(format_files(files))
    messages = [{"role": "system", "content": "\n\n".join(parts)}] if parts else []
    return messages + history_messages(history)
