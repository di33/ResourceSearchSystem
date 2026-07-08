from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ClientAuthConfig:
    client_keys: dict[str, tuple[str, ...]]
    admin_keys: tuple[str, ...] = ()
    debug: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.client_keys or self.admin_keys)


def split_csv(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        parts = raw.replace(";", ",").split(",")
    else:
        parts = []
        for item in raw:
            parts.extend(str(item or "").replace(";", ",").split(","))
    return tuple(part.strip() for part in parts if part.strip())


def parse_client_api_keys(raw: str | None) -> dict[str, tuple[str, ...]]:
    """Parse 'client-a:key1|key2,client-b:key3' into a client/key map."""
    result: dict[str, tuple[str, ...]] = {}
    for item in split_csv(raw or ""):
        if ":" not in item:
            continue
        client_id, _, keys_text = item.partition(":")
        client_id = client_id.strip()
        keys = tuple(key.strip() for key in keys_text.replace("|", ";").split(";") if key.strip())
        if client_id and keys:
            result[client_id] = keys
    return result


def extract_api_key(authorization: str | None = None, x_api_key: str | None = None) -> str:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    text = (authorization or "").strip()
    if not text:
        return ""
    scheme, _, token = text.partition(" ")
    if scheme.lower() in {"apikey", "bearer"} and token.strip():
        return token.strip()
    return ""


def _matches(candidate: str, keys: Iterable[str]) -> bool:
    return any(hmac.compare_digest(candidate, key) for key in keys)


def validate_client_api_key(client_id: str, api_key: str, config: ClientAuthConfig) -> bool:
    client_id = (client_id or "").strip()
    api_key = (api_key or "").strip()
    if not client_id:
        return False
    if config.debug and not config.configured:
        return True
    if not api_key:
        return False
    if _matches(api_key, config.admin_keys):
        return True
    return _matches(api_key, config.client_keys.get(client_id, ()))
