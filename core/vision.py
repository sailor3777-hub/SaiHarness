"""Описание изображений через vision-модель LM Studio (чистый Python, без Qt).

Инструмент vision_describe из tools_server.py отправляет картинку напрямую в OpenAI-совместимый API,
а не через агентский цикл обвязки: один запрос без инструментов, без потоков Qt и без повторного входа в чат.
Какая модель смотрит картинки, задаёт профиль, выбранный в настройке `vision_profile` (вкладка «Инструменты»):
из профиля берутся подключение, модель, температура и лимит токенов. Системный промпт профиля не используется:
он описывает персонажа для чата, а здесь нужен точный наблюдатель.
"""
import base64
import mimetypes
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import httpx

VISION_PROFILE_KEY = "vision_profile"   # имя профиля; пусто = ручные настройки ниже
VISION_URL_KEY, VISION_API_KEY_KEY, VISION_MODEL_KEY = "vision_url", "vision_api_key", "vision_model"
MANUAL_TEMPERATURE, MANUAL_MAX_TOKENS = 0.3, 4000
TIMEOUT = 600.0  # маленькая модель на слабом железе отвечает на картинку минутами
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
DEFAULT_QUESTION = "Опиши изображение подробно: что на нём изображено, весь видимый текст дословно, цвета и расположение."
SYSTEM_PROMPT = ("Ты — глаза для другой модели, которая сама не видит изображений. Отвечай по-русски, по существу и "
                 "только по тому, что действительно видно. Видимый текст переписывай дословно. Если что-то "
                 "разобрать нельзя, так и скажи, ничего не выдумывай.")


class VisionError(Exception):
    """Описать изображение не удалось; текст понятен и пользователю, и модели."""


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
    except sqlite3.Error:  # старая база без таблицы
        return default
    return row[0] if row else default


def load_config(db_path) -> VisionConfig:
    """Читает из базы профиль vision-модели и адрес его подключения. VisionError с подсказкой, что настроить."""
    if not db_path or not Path(db_path).exists():
        raise VisionError("База обвязки недоступна, vision не настроен.")
    con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        name = get_setting(con, VISION_PROFILE_KEY)
        if not name:
            url, model = get_setting(con, VISION_URL_KEY), get_setting(con, VISION_MODEL_KEY)
            if not (url and model):
                raise VisionError("Vision-модель не настроена: пользователь должен выбрать профиль или задать адрес и "
                                  "название модели вручную во вкладке «Инструменты» (блок «Vision»).")
            return VisionConfig(url, get_setting(con, VISION_API_KEY_KEY), model, MANUAL_TEMPERATURE,
                                MANUAL_MAX_TOKENS, "вручную")
        profile = con.execute("SELECT * FROM model_profiles WHERE name=?", (name,)).fetchone()
        if profile is None:
            raise VisionError(f"Профиль «{name}» для vision не найден: выбери другой во вкладке «Инструменты».")
        conn_row = con.execute("SELECT * FROM connections WHERE name=?", (profile["connection"],)).fetchone() \
            if profile["connection"] else None
        if conn_row is None:
            raise VisionError(f"У профиля «{name}» нет подключения: задай его во вкладке «Модель».")
    except sqlite3.Error as e:
        raise VisionError(f"Не удалось прочитать настройки vision ({e.__class__.__name__}).") from None
    finally:
        con.close()
    return VisionConfig(conn_row["url"], conn_row["api_key"], profile["base_model"],
                        float(profile["temperature"]), int(profile["max_tokens"]), name)


def image_data_url(path: Path) -> str:
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise VisionError(f"Это не поддерживаемое изображение: {path.name}. Форматы: "
                          f"{', '.join(sorted(e.lstrip('.') for e in IMAGE_EXTENSIONS))}.")
    try:
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise VisionError(f"Изображение слишком большое: {size // (1024 * 1024)} МБ, максимум "
                              f"{MAX_IMAGE_BYTES // (1024 * 1024)} МБ.")
        data = path.read_bytes()
    except OSError as e:
        raise VisionError(f"Файл не читается ({e.__class__.__name__}): {path}") from None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def describe_cached(cfg: VisionConfig, image: Path, question: str = "") -> str:
    """describe() с кешем рядом с файлом (<имя>.txt): картинка из истории чата описывается один раз.
    Кеш только для общего описания: у ответа на конкретный вопрос другая ценность."""
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
    """Один запрос к vision-модели; возвращает её ответ. Размышления модели отбрасываются."""
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
        raise VisionError(f"Нет связи с {cfg.base_url} (или модель не ответила вовремя).") from None
    except httpx.HTTPError as e:
        raise VisionError(f"Ошибка запроса к vision-модели: {e.__class__.__name__}") from None
    if r.status_code >= 400:
        try:
            err = r.json().get("error")
            message = err.get("message") if isinstance(err, dict) else err
        except (ValueError, AttributeError):
            message = None
        hint = " Возможно, у модели нет поддержки изображений." if r.status_code == 400 else ""
        raise VisionError(f"Vision-модель «{cfg.model}» вернула ошибку {r.status_code}: {message or 'без пояснения'}.{hint}")
    try:
        choice = r.json()["choices"][0]
        text = (choice["message"].get("content") or "").strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise VisionError("Vision-модель вернула некорректный ответ.") from None
    if not text:
        why = " Ответ оборван лимитом токенов: размышления съели его." if choice.get("finish_reason") == "length" else ""
        raise VisionError(f"Vision-модель «{cfg.model}» ответила пустым текстом.{why}")
    return text
