"""Image generation through ComfyUI (pure Python, no Qt).

The image_generate tool in tools_server.py takes a workflow in API format (Save (API Format) in ComfyUI, the same JSON
that is loaded into Open WebUI), puts the text, size and a random seed into it, sends it to `POST /prompt`, waits
for the result in `/history/<id>` and saves the picture to data/images.

There is no manual node mapping: the needed nodes are found by type and links. The text goes to the CLIPTextEncode
referenced by the KSampler's positive input (through intermediate nodes such as FluxGuidance), the size goes to the
empty latent node the sampler is connected to, seed and steps go to the KSampler itself. So any workflow of this shape works.

The server address is stored in app_settings (`comfy_url`), the workflow lives in data/comfy_workflow.json (the path
can be overridden with the SAI_COMFY_WORKFLOW variable).
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
MIN_SIDE, MAX_SIDE, MAX_PIXELS = 64, 1536, 1024 * 1024  # a GTX 1650 with 4 GB cannot do more than a megapixel
SIDE_STEP = 8                # the latent is divided by 8; a size from the model (200×200, 640×384) is kept as is if it is a multiple of 8
TOTAL_TIMEOUT = 900.0        # the first run loads models from disk, which takes minutes on a weak GPU
POLL_SECONDS = 1.0
SAMPLERS = ("KSampler", "KSamplerAdvanced")
TEXT_NODES = ("CLIPTextEncode",)
MAX_HOPS = 6                 # how many nodes to walk from the sampler's positive input to the text node


RESULT_PREFIX = "Image is ready and saved: "   # the chat finds the path in a tool result by this prefix and shows the picture
LEGACY_RESULT_PREFIXES: tuple[str, ...] = ("Изображение готово и сохранено: ",)  # earlier wordings (Russian version): pictures in old chats are shown too


def result_path(text: str) -> str:
    """Path to the picture from an image_generate result; empty if this is not a successful result."""
    for prefix in (RESULT_PREFIX, *LEGACY_RESULT_PREFIXES):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return ""


class ImageGenError(Exception):
    """Generation failed; the text is clear to both the user and the model."""


@dataclass
class ImageGenConfig:
    base_url: str
    workflow: dict


def normalize_url(raw: str) -> str:
    """'192.168.1.5:8188' → 'http://192.168.1.5:8188' (without a trailing slash)."""
    url = raw.strip().rstrip("/")
    if not url:
        raise ValueError("the address is empty")
    if "://" not in url:
        url = "http://" + url
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("an http:// or https:// address is required")
    if not httpx.URL(url).host:
        raise ValueError("the address has no host")
    return url


def load_workflow(path: Path | None = None) -> dict:
    path = Path(path or os.environ.get("SAI_COMFY_WORKFLOW") or WORKFLOW_PATH)
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ImageGenError(f"No workflow file: {path}. Save it from ComfyUI with Save (API Format).") from None
    except (OSError, ValueError) as e:
        raise ImageGenError(f"The workflow cannot be read ({e.__class__.__name__}): {path}") from None
    if not isinstance(workflow, dict) or not workflow or not all(isinstance(n, dict) and "class_type" in n
                                                                  for n in workflow.values()):
        raise ImageGenError("The workflow is not in API format: a JSON like {\"node id\": {class_type, inputs}} is required. "
                            "In ComfyUI this is Save (API Format), not the regular save.")
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
        raise ImageGenError("Image generation is not set up: the user must enter the ComfyUI address "
                            "on the Tools tab (the \"Images\" section).")
    return ImageGenConfig(url, load_workflow(workflow_path))


# --- parsing the workflow ---

def _link(value):
    """A link to another node's output in API format: ['id', index]."""
    return value[0] if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) else None


def find_nodes(workflow: dict) -> dict:
    """ids of the nodes that receive the values: sampler, text, latent. A missing node raises ImageGenError."""
    sampler_id = next((i for i, n in workflow.items() if n["class_type"] in SAMPLERS), None)
    if sampler_id is None:
        raise ImageGenError("The workflow has no KSampler node: it is unclear where to put the seed and steps.")
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
        raise ImageGenError("The workflow has no CLIPTextEncode text node connected to the KSampler's positive input.")
    latent_id = _link(inputs.get("latent_image"))
    latent = workflow.get(latent_id) if latent_id else None
    if latent is None or not {"width", "height"} <= set(latent.get("inputs", {})):
        raise ImageGenError("No empty latent node with width and height (EmptyLatentImage and similar) is connected to the KSampler.")
    return {"sampler": sampler_id, "text": node_id, "latent": latent_id}


def clamp_size(width: int, height: int) -> tuple[int, int]:
    """A side within [MIN_SIDE, MAX_SIDE], a multiple of SIDE_STEP; the area is at most MAX_PIXELS (proportions are kept)."""
    w, h = max(MIN_SIDE, min(MAX_SIDE, int(width))), max(MIN_SIDE, min(MAX_SIDE, int(height)))
    if w * h > MAX_PIXELS:
        k = (MAX_PIXELS / (w * h)) ** 0.5
        w, h = int(w * k), int(h * k)
    snap = lambda v: max(MIN_SIDE, v // SIDE_STEP * SIDE_STEP)
    return snap(w), snap(h)


def build_prompt(workflow: dict, text: str, width: int = DEFAULT_SIZE, height: int = DEFAULT_SIZE,
                 steps: int | None = None, seed: int | None = None) -> dict:
    """A copy of the workflow with the values filled in. Steps come from the workflow unless given explicitly."""
    if not text.strip():
        raise ImageGenError("prompt is empty: describe what to draw.")
    ids = find_nodes(workflow)
    out = copy.deepcopy(workflow)
    out[ids["text"]]["inputs"]["text"] = text.strip()
    out[ids["latent"]]["inputs"]["width"], out[ids["latent"]]["inputs"]["height"] = clamp_size(width, height)
    sampler = out[ids["sampler"]]["inputs"]
    sampler["noise_seed" if "noise_seed" in sampler else "seed"] = random.randrange(2 ** 48) if seed is None else int(seed)
    if steps:
        sampler["steps"] = max(1, min(int(steps), 60))
    return out


# --- talking to ComfyUI ---

def _node_errors(payload: dict) -> str:
    """Readable text from ComfyUI's reply to an invalid prompt (error + node_errors)."""
    err = payload.get("error")
    parts = [err.get("message") or err.get("type") or "" if isinstance(err, dict) else str(err or "")]
    for node_id, info in (payload.get("node_errors") or {}).items():
        for e in info.get("errors", []):
            parts.append(f"node {node_id} ({info.get('class_type')}): {e.get('message')} {e.get('details') or ''}".strip())
    return "; ".join(p for p in parts if p)[:600] or "no explanation"


def _history_error(entry: dict) -> str:
    for kind, data in entry.get("status", {}).get("messages", []):
        if kind == "execution_error":
            return f"{data.get('node_type', '')}: {data.get('exception_message', '')}".strip(": ").strip()[:600]
    return ""


def generate(cfg: ImageGenConfig, text: str, width: int = DEFAULT_SIZE, height: int = DEFAULT_SIZE,
             steps: int | None = None, seed: int | None = None, transport=None,
             timeout: float = TOTAL_TIMEOUT, poll: float = POLL_SECONDS) -> Path:
    """Draws a picture and returns the path to the file in data/images."""
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
                raise ImageGenError(f"ComfyUI did not accept the workflow: {detail}.")
            try:
                prompt_id = r.json()["prompt_id"]
            except (ValueError, KeyError, TypeError):
                raise ImageGenError("ComfyUI returned an unexpected reply to /prompt.") from None
            deadline = time.monotonic() + timeout
            while True:
                h = client.get(f"{base}/history/{prompt_id}")
                entry = (h.json() if h.status_code == 200 else {}).get(prompt_id)
                if entry and entry.get("status", {}).get("status_str") == "error":
                    raise ImageGenError(f"ComfyUI could not generate the image: {_history_error(entry) or 'execution error'}. "
                                        "If the GPU is busy with a model in LM Studio, there was not enough video memory.")
                if entry and entry.get("outputs"):
                    break
                if time.monotonic() > deadline:
                    raise ImageGenError(f"ComfyUI did not answer within {timeout:g} s.")
                time.sleep(poll)
            files = [i for out in entry["outputs"].values() for i in out.get("images", []) if i.get("type") != "temp"]
            if not files:
                raise ImageGenError("ComfyUI finished but returned no picture (is there no SaveImage in the workflow?).")
            f = files[0]
            v = client.get(f"{base}/view", params={"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                                   "type": f.get("type", "output")})
            if v.status_code != 200 or not v.content:
                raise ImageGenError(f"Could not fetch the picture from ComfyUI (HTTP {v.status_code}).")
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise ImageGenError(f"Cannot reach ComfyUI at {cfg.base_url}: it may be turned off.") from None
    except httpx.HTTPError as e:
        raise ImageGenError(f"Error talking to ComfyUI: {e.__class__.__name__}") from None
    ext = Path(f["filename"]).suffix.lower() or ".png"
    path = images.new_image_path(ext)
    try:
        path.write_bytes(v.content)
    except OSError as e:
        raise ImageGenError(f"Could not save the picture ({e.__class__.__name__}): {path}") from None
    return path
