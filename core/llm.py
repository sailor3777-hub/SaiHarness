import json
import threading
import uuid
from typing import Iterator

import httpx


LENGTH_NOTICE = "Лимит max tokens исчерпан, ответ оборван"


class LLMError(Exception):
    """Ошибка общения с сервером модели; текст можно показывать пользователю."""


def answer_in_reasoning(content: str, reasoning: str, notice: str = "") -> bool:
    """Ответ модели лежит в размышлениях: content пуст, а размышления завершены.

    Так делает, например, Qwen 3.5 в LM Studio: ответ приходит как reasoning_content, а content остаётся пустым.
    Если ответ оборван лимитом токенов, это недописанные размышления, ответом они не считаются.
    """
    return not content.strip() and bool(reasoning.strip()) and LENGTH_NOTICE not in notice


def _error_text(response: httpx.Response) -> str:
    """Достаёт сообщение об ошибке из тела ответа OpenAI-совместимого сервера."""
    code = response.status_code
    try:
        err = response.json().get("error")
        message = err.get("message") if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        message = None
    if code in (401, 403):
        return f"Сервер отклонил ключ ({code})"
    return f"Ошибка сервера {code}: {message}" if message else f"Ошибка сервера: {code}"


def get_context_info(base_url: str, api_key: str, model: str) -> tuple[int | None, bool, int | None]:
    """Размер контекста модели у LM Studio: (рабочий лимит, загружена ли модель, максимум).

    Рабочий лимит известен только у загруженной модели (loaded_context_length): при загрузке
    «на лету» LM Studio может выставить контекст намного меньше максимума модели.
    Любой сбой даёт (None, False, None): оценка лимита не должна ломать работу.
    """
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    try:
        r = httpx.get(
            f"{root}/api/v0/models", headers=_headers(api_key), timeout=4.0, trust_env=False
        )
        r.raise_for_status()
        for m in r.json().get("data", []):
            if isinstance(m, dict) and m.get("id") == model:
                maximum = m.get("max_context_length")
                if m.get("state") == "loaded" and m.get("loaded_context_length"):
                    return int(m["loaded_context_length"]), True, maximum
                return None, False, maximum
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    return None, False, None


def model_type(base_url: str, api_key: str, model: str, timeout: float = 4.0) -> str | None:
    """Тип модели у LM Studio: 'vlm' (видит изображения), 'llm' и т. д. Любой сбой или чужой сервер дают None."""
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    try:
        r = httpx.get(f"{root}/api/v0/models", headers=_headers(api_key), timeout=timeout, trust_env=False)
        r.raise_for_status()
        for m in r.json().get("data", []):
            if isinstance(m, dict) and m.get("id") == model:
                return m.get("type") if isinstance(m.get("type"), str) else None
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    return None


def _accumulate_tool_call(calls: dict[int, dict], tc: dict):
    """Склеивает вызов инструмента из кусков потока (имя и аргументы приходят фрагментами по index)."""
    if not isinstance(tc, dict):
        return
    idx = tc.get("index") if isinstance(tc.get("index"), int) else len(calls)
    entry = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        entry["id"] = tc["id"]
    fn = tc.get("function") or {}
    entry["name"] += fn.get("name") or ""
    args = fn.get("arguments")
    if isinstance(args, dict):  # некоторые серверы отдают объект вместо строки
        args = json.dumps(args, ensure_ascii=False)
    entry["arguments"] += args or ""
    if not entry["id"]:
        entry["id"] = f"call_{idx}_{uuid.uuid4().hex[:8]}"


def stream_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    cancel: threading.Event | None = None,
    tools: list[dict] | None = None,
) -> Iterator[tuple[str, object]]:
    """Стримит ответ модели. Отдаёт пары ('reasoning' | 'content', текст).

    Если модель решила вызвать инструменты, в конце потока отдаётся ('tool_calls', список) из словарей
    {id, name, arguments} (arguments — JSON-строка, собранная из кусков). Если сервер сообщил, сколько токенов
    занял запрос, отдаётся ('usage', {prompt_tokens, completion_tokens}).

    Размышления берутся из delta.reasoning_content (LM Studio, DeepSeek) или delta.reasoning.
    Если выставить cancel, генерация прекращается при ближайшем чанке от сервера.
    Предупреждения отдаются как ('notice', текст): сервер ответил другой моделью, чем запрошена
    (LM Studio подставляет загруженную при неизвестном имени), или ответ оборван лимитом токенов
    (у моделей с размышлениями лимит съедают сами размышления).
    """
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},  # сервер пришлёт настоящее число токенов запроса и ответа
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    # первый токен может ждать загрузки модели или обработки длинного промпта
    timeout = httpx.Timeout(10.0, read=300.0)
    checked_model = False
    calls: dict[int, dict] = {}
    try:
        with httpx.stream(
            "POST", f"{base_url}/chat/completions", json=body,
            headers=_headers(api_key), timeout=timeout, trust_env=False,
        ) as r:
            if r.status_code >= 400:
                r.read()
                raise LLMError(_error_text(r))
            for line in r.iter_lines():
                if cancel is not None and cancel.is_set():
                    return
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk:
                    err = chunk["error"]
                    raise LLMError(str(err.get("message") if isinstance(err, dict) else err))
                usage = chunk.get("usage")
                if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
                    yield "usage", {"prompt_tokens": usage["prompt_tokens"],
                                    "completion_tokens": int(usage.get("completion_tokens") or 0)}
                actual = chunk.get("model")
                if not checked_model and isinstance(actual, str):
                    checked_model = True
                    if actual != model:
                        yield "notice", f"Ответила модель «{actual}» вместо «{model}»"
                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason") == "length":
                        yield "notice", LENGTH_NOTICE
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield "reasoning", reasoning
                    content = delta.get("content")
                    if content:
                        yield "content", content
                    for tc in delta.get("tool_calls") or []:
                        _accumulate_tool_call(calls, tc)
        if calls:
            yield "tool_calls", [calls[i] for i in sorted(calls)]
    except (httpx.ConnectError, httpx.TimeoutException):
        raise LLMError(f"Нет связи с {base_url}") from None
    except httpx.HTTPError as e:
        raise LLMError(f"Ошибка запроса: {e}") from None


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def list_models(base_url: str, api_key: str = "", timeout: float = 5.0) -> list[str]:
    """Возвращает id моделей с OpenAI-совместимого сервера (GET {base_url}/models)."""
    try:
        r = httpx.get(
            f"{base_url}/models", headers=_headers(api_key), timeout=timeout, trust_env=False
        )
        r.raise_for_status()
        data = r.json()
    except (httpx.ConnectError, httpx.TimeoutException):
        raise LLMError(f"Нет связи с {base_url}") from None
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            raise LLMError(f"Сервер отклонил ключ ({code})") from None
        raise LLMError(f"Ошибка сервера: {code}") from None
    except httpx.HTTPError as e:
        raise LLMError(f"Ошибка запроса: {e}") from None
    except ValueError:
        raise LLMError("Некорректный ответ сервера") from None
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LLMError("Некорректный ответ сервера")
    return sorted(m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), str))
