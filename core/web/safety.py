"""Проверка адресов перед сетевым запросом: только http/https и только публичные узлы (защита от SSRF)."""
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrl(ValueError):
    """Адрес нельзя загружать; текст ошибки показывается модели."""


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_global


def host_blocked(host: str, blocklist) -> bool:
    host = host.lower().rstrip(".")
    return any(host == b or host.endswith("." + b) for b in (x.lower().strip().lstrip(".") for x in blocklist) if b)


def check_url(url: str, allow_private: bool = False, blocklist=(), resolve=socket.getaddrinfo) -> str:
    """Возвращает нормализованный URL или бросает UnsafeUrl. resolve подменяется в тестах."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl(f"Разрешены только http и https, получено: {parts.scheme or 'пусто'}://")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("В адресе нет имени узла")
    if parts.username or parts.password:
        raise UnsafeUrl("Адреса с логином и паролем не поддерживаются")
    if host_blocked(host, blocklist):
        raise UnsafeUrl(f"Узел {host} в блок-листе")
    if not allow_private:
        try:
            infos = resolve(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise UnsafeUrl(f"Не удалось определить адрес узла {host}: {e}") from None
        ips = {i[4][0] for i in infos}
        if not ips:
            raise UnsafeUrl(f"Узел {host} не имеет адресов")
        for ip in ips:
            if not _is_public(ip):
                raise UnsafeUrl(f"Узел {host} указывает на непубличный адрес ({ip}): локальные и частные сети закрыты")
    return parts.geturl()
