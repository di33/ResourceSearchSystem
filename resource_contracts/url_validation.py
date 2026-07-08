from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from resource_contracts.auth import split_csv

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


class UrlValidationError(ValueError):
    pass


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    for allowed in allowed_hosts:
        allowed = allowed.lower().rstrip(".")
        if not allowed:
            continue
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if host.endswith(suffix):
                return True
        elif host == allowed:
            return True
    return False


def _is_private_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host in _BLOCKED_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        pass

    return False


def validate_signed_source_url(
    url: str,
    *,
    allowed_hosts: str | tuple[str, ...],
    allow_private_hosts: bool = False,
) -> str:
    text = str(url or "").strip()
    if not text:
        raise UrlValidationError("source_object_url must not be blank")
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UrlValidationError("source_object_url must use http or https")
    if not parsed.hostname:
        raise UrlValidationError("source_object_url host is required")
    hosts = allowed_hosts if isinstance(allowed_hosts, tuple) else split_csv(allowed_hosts)
    if hosts and not _host_allowed(parsed.hostname, hosts):
        raise UrlValidationError(f"source_object_url host is not allowed: {parsed.hostname}")
    if not hosts:
        raise UrlValidationError("source URL host allowlist is not configured")
    if not allow_private_hosts and _is_private_host(parsed.hostname):
        raise UrlValidationError(f"source_object_url private host is not allowed: {parsed.hostname}")
    return text
