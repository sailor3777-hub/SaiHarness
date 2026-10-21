"""Address checks before a network request: only http/https and only public hosts (SSRF protection)."""
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrl(ValueError):
    """The address must not be loaded; the error text is shown to the model."""


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_global


def host_blocked(host: str, blocklist) -> bool:
    host = host.lower().rstrip(".")
    return any(host == b or host.endswith("." + b) for b in (x.lower().strip().lstrip(".") for x in blocklist) if b)


def check_url(url: str, allow_private: bool = False, blocklist=(), resolve=socket.getaddrinfo) -> str:
    """Returns the normalized URL or raises UnsafeUrl. resolve is replaced in tests."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl(f"Only http and https are allowed, got: {parts.scheme or 'empty'}://")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("The address has no host name")
    if parts.username or parts.password:
        raise UnsafeUrl("Addresses with a login and password are not supported")
    if host_blocked(host, blocklist):
        raise UnsafeUrl(f"Host {host} is on the block list")
    if not allow_private:
        try:
            infos = resolve(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise UnsafeUrl(f"Could not resolve the address of host {host}: {e}") from None
        ips = {i[4][0] for i in infos}
        if not ips:
            raise UnsafeUrl(f"Host {host} has no addresses")
        for ip in ips:
            if not _is_public(ip):
                raise UnsafeUrl(f"Host {host} points to a non-public address ({ip}): local and private networks are blocked")
    return parts.geturl()
