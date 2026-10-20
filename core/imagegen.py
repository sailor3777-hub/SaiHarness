"""Генерация изображений через ComfyUI (чистый Python, без Qt).

Инструмент image_generate из tools_server.py берёт workflow в API-формате (Save (API Format) в ComfyUI, тот же JSON,
что загружен в Open WebUI), подставляет в него текст, размер и случайный seed, отправляет на `POST /prompt`, ждёт
результат в `/history/<id>` и сохраняет картинку в data/images.

Сопоставления узлов вручную нет: нужные узлы находятся по типу и связям. Текст идёт в CLIPTextEncode, на который
ссылается positive у KSampler (через промежуточные узлы вроде FluxGuidance), размер — в узел пустого латента, к
которому подключён сэмплер, seed и шаги — в сам KSampler. Поэтому подойдёт любой workflow такой формы.

Адрес сервера хранится в app_settings (`comfy_url`), workflow лежит в data/comfy_workflow.json (путь можно
переопределить переменной SAI_COMFY_WORKFLOW).
"""
import copy
import json
import os
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from core import images

COMFY_URL_KEY = "comfy_url"
WORKFLOW_PATH = Path(__file__).resolve().parent.parent / "data" / "comfy_workflow.json"
DEFAULT_SIZE = 512
MIN_SIDE, MAX_SIDE, MAX_PIXELS = 64, 1536, 1024 * 1024  # GTX 1650 с 4 ГБ: больше мегапикселя не вытянуть
SIDE_STEP = 8                # латент делится на 8; размер от модели (200×200, 640×384) сохраняется как есть, если кратен 8
TOTAL_TIMEOUT = 900.0        # первый запуск грузит модели с диска, на слабой карте это минуты
POLL_SECONDS = 1.0
SAMPLERS = ("KSampler", "KSamplerAdvanced")
TEXT_NODES = ("CLIPTextEncode",)
MAX_HOPS = 6                 # сколько узлов проходим от positive сэмплера до текстового


RESULT_PREFIX = "Изображение готово и сохранено: "   # по нему чат находит путь в результате инструмента и показывает картинку


def result_path(text: str) -> str:
    """Путь к картинке из результата image_generate; пусто, если это не успешный результат."""
    return text.split(RESULT_PREFIX, 1)[1].strip() if text.startswith(RESULT_PREFIX) else ""


class ImageGenError(Exception):
    """Сгенерировать не удалось; текст понятен и пользователю, и модели."""


@dataclass
class ImageGenConfig:
    base_url: str
    workflow: dict


def normalize_url(raw: str) -> str:
    """'192.168.1.5:8188' → 'http://192.168.1.5:8188' (без хвостового слэша)."""
    url = raw.strip().rstrip("/")
    if not url:
        raise ValueError("адрес пуст")
    if "://" not in url:
        url = "http://" + url
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("нужен адрес http:// или https://")
    if not httpx.URL(url).host:
        raise ValueError("в адресе нет хоста")
    return url


def load_workflow(path: Path | None = None) -> dict:
    path = Path(path or os.environ.get("SAI_COMFY_WORKFLOW") or WORKFLOW_PATH)
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ImageGenError(f"Нет файла workflow: {path}. Сохрани его из ComfyUI через Save (API Format).") from None
    except (OSError, ValueError) as e:
        raise ImageGenError(f"Workflow не читается ({e.__class__.__name__}): {path}") from None
    if not isinstance(workflow, dict) or not workflow or not all(isinstance(n, dict) and "class_type" in n
                                                                  for n in workflow.values()):
        raise ImageGenError("Workflow не в API-формате: нужен JSON вида {«id узла»: {class_type, inputs}}. "
                            "В ComfyUI это Save (API Format), а не обычное сохранение.")
    return workflow


def load_config(db_path, workflow_path: Path | None = None) -> ImageGenConfig:
    url = ""
    if db_path and Path(db_path).exists():
        con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (COMFY_URL_KEY,)).fetchone()
            url = row[0] if row else ""
        except sqlite3.Error:
            url = ""
        finally:
            con.close()
    if not url:
        raise ImageGenError("Генерация изображений не настроена: пользователь должен задать адрес ComfyUI "
                            "во вкладке «Инструменты» (блок «Картинки»).")
    return ImageGenConfig(url, load_workflow(workflow_path))


# --- разбор workflow ---

def _link(value):
    """Ссылка на выход другого узла в API-формате: ['id', индекс]."""
    return value[0] if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) else None


def find_nodes(workflow: dict) -> dict:
    """id узлов, куда подставляются значения: sampler, text, latent. Нет нужного узла: ImageGenError."""
    sampler_id = next((i for i, n in workflow.items() if n["class_type"] in SAMPLERS), None)
    if sampler_id is None:
        raise ImageGenError("В workflow нет узла KSampler: не понятно, куда подставлять seed и шаги.")
    inputs = workflow[sampler_id].get("inputs", {})
    node_id = _link(inputs.get("positive"))
    for _ in range(MAX_HOPS):
        node = workflow.get(node_id) if node_id else None
        if node is None or node["class_type"] in TEXT_NODES:
            break
        node_inputs = node.get("inputs", {})
        node_id = _link(node_inputs.get("conditioning")) or next(
            (l for l in map(_link, node_inputs.values()) if l in workflow), None)
    if not node_id or workflow.get(node_id, {}).get("class_type") not in TEXT_NODES:
        raise ImageGenError("В workflow не нашёлся текстовый узел CLIPTextEncode, подключённый к positive у KSampler.")
    latent_id = _link(inputs.get("latent_image"))
    latent = workflow.get(latent_id) if latent_id else None
    if latent is None or not {"width", "height"} <= set(latent.get("inputs", {})):
        raise ImageGenError("К KSampler не подключён узел пустого латента с width и height (EmptyLatentImage и подобные).")
    return {"sampler": sampler_id, "text": node_id, "latent": latent_id}


def clamp_size(width: int, height: int) -> tuple[int, int]:
    """Сторона в [MIN_SIDE, MAX_SIDE], кратная SIDE_STEP; площадь не больше MAX_PIXELS (пропорции сохраняются)."""
    w, h = max(MIN_SIDE, min(MAX_SIDE, int(width))), max(MIN_SIDE, min(MAX_SIDE, int(height)))
    if w * h > MAX_PIXELS:
        k = (MAX_PIXELS / (w * h)) ** 0.5
        w, h = int(w * k), int(h * k)
    snap = lambda v: max(MIN_SIDE, v // SIDE_STEP * SIDE_STEP)
    return snap(w), snap(h)


def build_prompt(workflow: dict, text: str, width: int = DEFAULT_SIZE, height: int = DEFAULT_SIZE,
                 steps: int | None = None, seed: int | None = None) -> dict:
    """Копия workflow с подставленными значениями. Шаги берутся из workflow, пока не заданы явно."""
    if not text.strip():
        raise ImageGenError("prompt пуст: опиши, что нарисовать.")
    ids = find_nodes(workflow)
    out = copy.deepcopy(workflow)
    out[ids["text"]]["inputs"]["text"] = text.strip()
    out[ids["latent"]]["inputs"]["width"], out[ids["latent"]]["inputs"]["height"] = clamp_size(width, height)
    sampler = out[ids["sampler"]]["inputs"]
    sampler["noise_seed" if "noise_seed" in sampler else "seed"] = random.randrange(2 ** 48) if seed is None else int(seed)
    if steps:
        sampler["steps"] = max(1, min(int(steps), 60))
    return out


# --- общение с ComfyUI ---

def _node_errors(payload: dict) -> str:
    """Человеческий текст из ответа ComfyUI на невалидный prompt (error + node_errors)."""
    err = payload.get("error")
    parts = [err.get("message") or err.get("type") or "" if isinstance(err, dict) else str(err or "")]
    for node_id, info in (payload.get("node_errors") or {}).items():
        for e in info.get("errors", []):
            parts.append(f"узел {node_id} ({info.get('class_type')}): {e.get('message')} {e.get('details') or ''}".strip())
    return "; ".join(p for p in parts if p)[:600] or "без пояснения"


def _history_error(entry: dict) -> str:
    for kind, data in entry.get("status", {}).get("messages", []):
        if kind == "execution_error":
            return f"{data.get('node_type', '')}: {data.get('exception_message', '')}".strip(": ").strip()[:600]
    return ""


def generate(cfg: ImageGenConfig, text: str, width: int = DEFAULT_SIZE, height: int = DEFAULT_SIZE,
             steps: int | None = None, seed: int | None = None, transport=None,
             timeout: float = TOTAL_TIMEOUT, poll: float = POLL_SECONDS) -> Path:
    """Рисует картинку и возвращает путь к файлу в data/images."""
    prompt = build_prompt(cfg.workflow, text, width, height, steps, seed)
    base = cfg.base_url.rstrip("/")
    try:
        with httpx.Client(transport=transport, trust_env=False, timeout=httpx.Timeout(15.0, read=60.0)) as client:
            r = client.post(f"{base}/prompt", json={"prompt": prompt, "client_id": uuid.uuid4().hex})
            if r.status_code >= 400:
                try:
                    detail = _node_errors(r.json())
                except ValueError:
                    detail = f"HTTP {r.status_code}"
                raise ImageGenError(f"ComfyUI не принял workflow: {detail}.")
            try:
                prompt_id = r.json()["prompt_id"]
            except (ValueError, KeyError, TypeError):
                raise ImageGenError("ComfyUI вернул непонятный ответ на /prompt.") from None
            deadline = time.monotonic() + timeout
            while True:
                h = client.get(f"{base}/history/{prompt_id}")
                entry = (h.json() if h.status_code == 200 else {}).get(prompt_id)
                if entry and entry.get("status", {}).get("status_str") == "error":
                    raise ImageGenError(f"ComfyUI не смог сгенерировать: {_history_error(entry) or 'ошибка выполнения'}. "
                                        "Если видеокарта занята моделью в LM Studio, не хватило видеопамяти.")
                if entry and entry.get("outputs"):
                    break
                if time.monotonic() > deadline:
                    raise ImageGenError(f"ComfyUI не ответил за {timeout:g} с.")
                time.sleep(poll)
            files = [i for out in entry["outputs"].values() for i in out.get("images", []) if i.get("type") != "temp"]
            if not files:
                raise ImageGenError("ComfyUI завершил работу, но не отдал картинку (в workflow нет SaveImage?).")
            f = files[0]
            v = client.get(f"{base}/view", params={"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                                   "type": f.get("type", "output")})
            if v.status_code != 200 or not v.content:
                raise ImageGenError(f"Не удалось забрать картинку у ComfyUI (HTTP {v.status_code}).")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise ImageGenError(f"Нет связи с ComfyUI по адресу {cfg.base_url}: возможно, он выключен.") from None
    except httpx.HTTPError as e:
        raise ImageGenError(f"Ошибка связи с ComfyUI: {e.__class__.__name__}") from None
    ext = Path(f["filename"]).suffix.lower() or ".png"
    path = images.new_image_path(ext)
    try:
        path.write_bytes(v.content)
    except OSError as e:
        raise ImageGenError(f"Не удалось сохранить картинку ({e.__class__.__name__}): {path}") from None
    return path
