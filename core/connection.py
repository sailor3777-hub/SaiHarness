from urllib.parse import urlsplit

KINDS = {"LM Studio": 1234}  # connection type -> default port


def normalize_url(raw: str, default_port: int = 1234) -> str:
    """Turns the user's input into the base address of an OpenAI-compatible API.

    '192.168.1.5'              -> http://192.168.1.5:1234/v1
    'localhost:1234'           -> http://localhost:1234/v1
    'http://host:8080'         -> http://host:8080/v1
    'https://host/custom/v1/'  -> https://host/custom/v1
    The default port is added only if no scheme is given and there is no port.
    Raises ValueError if the address cannot be parsed.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty address")
    has_scheme = "://" in raw
    parts = urlsplit(raw if has_scheme else f"http://{raw}")
    if parts.scheme not in ("http", "https"):
        raise ValueError("Only http and https are supported")
    if not parts.hostname:
        raise ValueError("No host found")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Invalid port") from None
    if port is None and not has_scheme:
        port = default_port
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/") or "/v1"
    return f"{parts.scheme}://{netloc}{path}"
