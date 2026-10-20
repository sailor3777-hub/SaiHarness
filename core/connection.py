from urllib.parse import urlsplit

KINDS = {"LM Studio": 1234}  # тип подключения -> порт по умолчанию


def normalize_url(raw: str, default_port: int = 1234) -> str:
    """Приводит ввод пользователя к базовому адресу OpenAI-совместимого API.

    '192.168.1.5'              -> http://192.168.1.5:1234/v1
    'localhost:1234'           -> http://localhost:1234/v1
    'http://host:8080'         -> http://host:8080/v1
    'https://host/custom/v1/'  -> https://host/custom/v1
    Порт по умолчанию подставляется только если схема не указана и порта нет.
    Бросает ValueError, если адрес разобрать нельзя.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Пустой адрес")
    has_scheme = "://" in raw
    parts = urlsplit(raw if has_scheme else f"http://{raw}")
    if parts.scheme not in ("http", "https"):
        raise ValueError("Поддерживаются только http и https")
    if not parts.hostname:
        raise ValueError("Не найден хост")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Некорректный порт") from None
    if port is None and not has_scheme:
        port = default_port
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/") or "/v1"
    return f"{parts.scheme}://{netloc}{path}"
