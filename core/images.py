"""Images in chat messages (pure Python, no Qt).

An image is stored as a file in data/images; the message holds only the path. In a request to the model it first sits
as a "placeholder" (an image_url part with a sai-image:path link): this way building the request and estimating tokens
do not read or encode files on every interface update. Right before sending (in the reply thread) placeholders are
replaced with real data URLs if the model can see images, or with a description from the vision model if it cannot.
"""
import json
import os
import uuid
from pathlib import Path
from typing import Callable

IMAGES_DIR = Path(os.environ.get("SAI_IMAGES_DIR") or Path(__file__).resolve().parent.parent / "data" / "images")  # the variable is for tests
PLACEHOLDER_PREFIX = "sai-image:"
MAX_IMAGES_PER_MESSAGE = 4
MAX_SIDE = 1280              # long side when saving: a larger picture wastes context
IMAGE_TOKENS = 1200          # rough estimate for one picture; the server's measurement corrects the real number
HISTORY_IMAGE_MESSAGES = 2   # images of how many latest messages go to the model; the rest keep a note


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
    """Path from a placeholder part (otherwise None)."""
    if isinstance(part, dict) and part.get("type") == "image_url":
        url = (part.get("image_url") or {}).get("url")
        if isinstance(url, str) and url.startswith(PLACEHOLDER_PREFIX):
            return url[len(PLACEHOLDER_PREFIX):]
    return None


def has_placeholders(messages: list[dict]) -> bool:
    return any(placeholder_path(p) is not None for m in messages if isinstance(m.get("content"), list)
               for p in m["content"])


def text_of(content) -> str:
    """Message text; for a message made of parts, only the text parts."""
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def count_images(content) -> int:
    return sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url") if isinstance(content, list) else 0


def user_content(text: str, paths: list[str]):
    """Content of a user message: a string or a list of parts (text and image placeholders)."""
    if not paths:
        return text
    parts = [{"type": "text", "text": text}] if text else []
    return parts + [placeholder_part(p) for p in paths]


def to_data_urls(messages: list[dict]) -> list[dict]:
    """Placeholders -> real data URLs (for a model that can see images)."""
    from core.vision import image_data_url
    return _map_images(messages, lambda path: {"type": "image_url", "image_url": {"url": image_data_url(Path(path))}})


def to_descriptions(messages: list[dict], describe: Callable[[Path], str]) -> list[dict]:
    """Placeholders -> description text (for a model without vision)."""
    def part(path: str) -> dict:
        return {"type": "text", "text": f"[Image {Path(path).name}. Described by the vision model: {describe(Path(path))}]"}
    out = _map_images(messages, part)
    for m in out:  # a plain string is assembled from text-only parts: this works with any template
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
