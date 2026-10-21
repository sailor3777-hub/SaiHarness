"""Image description through an LM Studio vision model (pure Python, no Qt).

The vision_describe tool in tools_server.py sends the picture straight to the OpenAI-compatible API,
not through the harness's agent loop: one request without tools, without Qt threads and without re-entering the chat.
Which model looks at pictures is set by the profile selected in the `vision_profile` setting (Tools tab):
the profile supplies the connection, model, temperature and token limit. The profile's system prompt is not used:
it describes a chat persona, while here an exact observer is needed.
"""
import base64
import mimetypes
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

VISION_PROFILE_KEY = "vision_profile"   # profile name; empty = the manual settings below
VISION_URL_KEY, VISION_API_KEY_KEY, VISION_MODEL_KEY = "vision_url", "vision_api_key", "vision_model"
MANUAL_TEMPERATURE, MANUAL_MAX_TOKENS = 0.3, 4000
TIMEOUT = 600.0  # a small model on weak hardware takes minutes to answer about a picture
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
DEFAULT_QUESTION = "Describe the image in detail: what it shows, all visible text verbatim, colors and layout."
SYSTEM_PROMPT = ("You are the eyes of another model that cannot see images itself. Answer in English, to the point, "
                 "and only about what is actually visible. Copy visible text verbatim. If something "
                 "cannot be made out, say so and do not make anything up.")


class VisionError(Exception):
    """Describing the image failed; the text is clear to both the user and the model."""


@dataclass
class VisionConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    profile: str


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:  # an old database without the table
        return default
    return row[0] if row else default


def load_config(db_path) -> VisionConfig:
    """Reads the vision model profile and its connection address from the database. VisionError says what to set up."""
    if not db_path or not Path(db_path).exists():
        raise VisionError("The harness database is unavailable, vision is not set up.")
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        name = get_setting(con, VISION_PROFILE_KEY)
        if not name:
            url, model = get_setting(con, VISION_URL_KEY), get_setting(con, VISION_MODEL_KEY)
            if not (url and model):
                raise VisionError("The vision model is not set up: the user must select a profile or enter the address and "
                                  "model name manually on the Tools tab (the \"Vision\" section).")
            return VisionConfig(url, get_setting(con, VISION_API_KEY_KEY), model, MANUAL_TEMPERATURE,
                                MANUAL_MAX_TOKENS, "manual")
        profile = con.execute("SELECT * FROM model_profiles WHERE name=?", (name,)).fetchone()
        if profile is None:
            raise VisionError(f"Vision profile \"{name}\" not found: choose another one on the Tools tab.")
        conn_row = con.execute("SELECT * FROM connections WHERE name=?", (profile["connection"],)).fetchone() \
            if profile["connection"] else None
        if conn_row is None:
            raise VisionError(f"Profile \"{name}\" has no connection: set it on the Model tab.")
    except sqlite3.Error as e:
        raise VisionError(f"Could not read the vision settings ({e.__class__.__name__}).") from None
    finally:
        con.close()
    return VisionConfig(conn_row["url"], conn_row["api_key"], profile["base_model"],
                        float(profile["temperature"]), int(profile["max_tokens"]), name)


def image_data_url(path: Path) -> str:
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise VisionError(f"Not a supported image: {path.name}. Formats: "
                          f"{', '.join(sorted(e.lstrip('.') for e in IMAGE_EXTENSIONS))}.")
    try:
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise VisionError(f"The image is too large: {size // (1024 * 1024)} MB, the maximum is "
                              f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
        data = path.read_bytes()
    except OSError as e:
        raise VisionError(f"The file cannot be read ({e.__class__.__name__}): {path}") from None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def describe_cached(cfg: VisionConfig, image: Path, question: str = "") -> str:
    """describe() with a cache next to the file (<name>.txt): a picture from the chat history is described once.
    Only the general description is cached: an answer to a specific question has a different value."""
    cache = image.with_name(image.name + ".txt")
    if not question.strip():
        try:
            if cache.exists() and cache.stat().st_mtime >= image.stat().st_mtime:
                return cache.read_text(encoding="utf-8")
        except OSError:
            pass
    text = describe(cfg, image, question)
    if not question.strip():
        try:
            cache.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text


def describe(cfg: VisionConfig, image: Path, question: str = "", transport=None, timeout: float = TIMEOUT) -> str:
    """One request to the vision model; returns its answer. The model's reasoning is discarded."""
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": question.strip() or DEFAULT_QUESTION},
                {"type": "image_url", "image_url": {"url": image_data_url(image)}},
            ]},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        with httpx.Client(transport=transport, trust_env=False, timeout=httpx.Timeout(10.0, read=timeout)) as client:
            r = client.post(f"{cfg.base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException):
        raise VisionError(f"Cannot reach {cfg.base_url} (or the model did not answer in time).") from None
    except httpx.HTTPError as e:
        raise VisionError(f"Vision model request error: {e.__class__.__name__}") from None
    if r.status_code >= 400:
        try:
            err = r.json().get("error")
            message = err.get("message") if isinstance(err, dict) else err
        except (ValueError, AttributeError):
            message = None
        hint = " The model may not support images." if r.status_code == 400 else ""
        raise VisionError(f"Vision model \"{cfg.model}\" returned error {r.status_code}: {message or 'no explanation'}.{hint}")
    try:
        choice = r.json()["choices"][0]
        text = (choice["message"].get("content") or "").strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise VisionError("The vision model returned an invalid reply.") from None
    if not text:
        why = " The reply was cut off by the token limit: the reasoning used it up." if choice.get("finish_reason") == "length" else ""
        raise VisionError(f"Vision model \"{cfg.model}\" returned empty text.{why}")
    return text
