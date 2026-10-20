"""Изображения в сообщениях чата (чистый Python, без Qt).

Картинка хранится файлом в data/images, в сообщении записан только путь. В запросе к модели она сначала лежит
«заглушкой» (часть image_url со ссылкой sai-image:путь): так сборка и оценка токенов не читают и не кодируют файлы
при каждом обновлении интерфейса. Прямо перед отправкой (в потоке ответа) заглушки заменяются настоящими data-URL,
если модель видит изображения, или описанием от vision-модели, если не видит.
"""
import json
import os
import uuid
from pathlib import Path
from typing import Callable

IMAGES_DIR = Path(os.environ.get("SAI_IMAGES_DIR") or Path(__file__).resolve().parent.parent / "data" / "images")  # переменная нужна тестам
PLACEHOLDER_PREFIX = "sai-image:"
MAX_IMAGES_PER_MESSAGE = 4
MAX_SIDE = 1280              # длинная сторона при сохранении: картинка крупнее съедает контекст зря
IMAGE_TOKENS = 1200          # грубая оценка одной картинки; настоящее число поправляет замер сервера
HISTORY_IMAGE_MESSAGES = 2   # картинки скольких последних сообщений уходят модели; в остальных остаётся пометка


def new_image_path(ext: str = ".png") -> Path:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGES_DIR / f"{uuid.uuid4().hex[:16]}{ext}"


def encode_paths(paths) -> str:
    return json.dumps([str(p) for p in paths], ensure_ascii=False) if paths else ""


def decode_paths(raw: str) -> list[str]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def placeholder_part(path: str) -> dict:
    return {"type": "image_url", "image_url": {"url": PLACEHOLDER_PREFIX + str(path)}}


def placeholder_path(part) -> str | None:
    """Путь из части-заглушки (иначе None)."""
    if isinstance(part, dict) and part.get("type") == "image_url":
        url = (part.get("image_url") or {}).get("url")
        if isinstance(url, str) and url.startswith(PLACEHOLDER_PREFIX):
            return url[len(PLACEHOLDER_PREFIX):]
    return None


def has_placeholders(messages: list[dict]) -> bool:
    return any(placeholder_path(p) is not None for m in messages if isinstance(m.get("content"), list)
               for p in m["content"])


def text_of(content) -> str:
    """Текст сообщения; у сообщения из частей — только текстовые части."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def count_images(content) -> int:
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url") if isinstance(content, list) else 0


def user_content(text: str, paths: list[str]):
    """Содержимое сообщения пользователя: строка или список частей (текст и заглушки картинок)."""
    if not paths:
        return text
    parts = [{"type": "text", "text": text}] if text else []
    return parts + [placeholder_part(p) for p in paths]


def to_data_urls(messages: list[dict]) -> list[dict]:
    """Заглушки -> настоящие data-URL (для модели, которая видит изображения)."""
    from core.vision import image_data_url
    return _map_images(messages, lambda path: {"type": "image_url", "image_url": {"url": image_data_url(Path(path))}})


def to_descriptions(messages: list[dict], describe: Callable[[Path], str]) -> list[dict]:
    """Заглушки -> текст описания (для модели без зрения)."""
    def part(path: str) -> dict:
        return {"type": "text", "text": f"[Изображение {Path(path).name}. Его описала vision-модель: {describe(Path(path))}]"}
    out = _map_images(messages, part)
    for m in out:  # из одних текстовых частей собираем обычную строку: так совместимы любые шаблоны
        if isinstance(m.get("content"), list) and all(p.get("type") == "text" for p in m["content"]):
            m["content"] = text_of(m["content"])
    return out


def _map_images(messages: list[dict], make: Callable[[str], dict]) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            m = {**m, "content": [make(path) if (path := placeholder_path(p)) is not None else p for p in m["content"]]}
        out.append(m)
    return out
