"""Shared object-storage profile configuration.

A storage profile describes how an object key maps to a physical bucket and
how read URLs should be generated. Databases and manifests should store the
stable profile id plus object_key, while bucket/CDN/auth details stay in
environment or secret-backed configuration.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


PROFILE_FILE_ENV_NAMES = (
    "STORAGE_PROFILES_FILE",
    "OBJECT_STORAGE_PROFILES_FILE",
    "SERVER_STORAGE_PROFILES_FILE",
    "RP_STORAGE_PROFILES_FILE",
)

PROFILE_JSON_ENV_NAMES = (
    "STORAGE_PROFILES_JSON",
    "OBJECT_STORAGE_PROFILES_JSON",
    "SERVER_STORAGE_PROFILES_JSON",
    "RP_STORAGE_PROFILES_JSON",
)

DEFAULT_PROFILE_ENV_NAMES = (
    "STORAGE_PROFILE_ID",
    "OBJECT_STORAGE_PROFILE_ID",
    "SERVER_STORAGE_PROFILE_ID",
    "RP_STORAGE_PROFILE_ID",
)


def _csv_items(raw: str | None) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _string_items(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if isinstance(value, str):
        items = _csv_items(value)
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = []
    return tuple(items or default)


def _env_first(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = env.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _strip_json_comments(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        ch = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            index += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            index += 1
            continue
        if ch == "/" and nxt == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if ch == "/" and nxt == "*":
            index += 2
            while index + 1 < len(text) and not (text[index] == "*" and text[index + 1] == "/"):
                if text[index] in "\r\n":
                    result.append(text[index])
                index += 1
            index += 2
            continue
        result.append(ch)
        index += 1
    return "".join(result)


def _remove_json_trailing_commas(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        ch = text[index]
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            index += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            index += 1
            continue
        if ch == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                index += 1
                continue
        result.append(ch)
        index += 1
    return "".join(result)


def _loads_jsonc(text: str) -> Any:
    return json.loads(_remove_json_trailing_commas(_strip_json_comments(text)))


def _resolve_env_refs(data: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """Resolve config values like {"access_key_env": "OBJECT_STORAGE_ACCESS_KEY"}."""
    resolved = dict(data)
    for key in list(data):
        if not key.endswith("_env"):
            continue
        target = key[:-4]
        env_names = data.get(key)
        if isinstance(env_names, str):
            env_names = [env_names]
        if target == "cdn_auth_key":
            values = []
            for env_name in env_names or []:
                name = str(env_name or "").strip()
                if name and env.get(name):
                    values.append(str(env.get(name) or ""))
            resolved["cdn_auth_keys"] = values
            resolved[target] = values[0] if values else ""
            resolved.pop(key, None)
            continue
        value = ""
        for env_name in env_names or []:
            name = str(env_name or "").strip()
            if name and env.get(name):
                value = str(env.get(name) or "")
                break
        resolved[target] = value
        resolved.pop(key, None)
    return resolved


@dataclass(frozen=True)
class StorageProfile:
    profile_id: str
    bucket: str
    endpoint: str = ""
    public_endpoint: str = ""
    cdn_endpoint: str = ""
    access_key: str = ""
    secret_key: str = ""
    region: str = ""
    signature_version: str = "s3v4"
    addressing_style: str = "auto"
    url_mode: str = "s3_presign"
    path_template: str = "/{key}"
    allowed_prefixes: tuple[str, ...] = field(default_factory=tuple)
    cdn_auth_type: str = "A"
    cdn_auth_algorithm: str = "md5"
    cdn_auth_sign_param: str = "sign"
    cdn_auth_expires: int = 86400
    cdn_auth_time_format: str = "unix_decimal"
    cdn_auth_scope: str = "file_suffix"
    cdn_auth_file_suffixes: tuple[str, ...] = ("*",)
    cdn_auth_uid: str = "0"
    cdn_auth_rand: str = "0"
    cdn_auth_key: str = ""
    cdn_auth_keys: tuple[str, ...] = field(default_factory=tuple)

    def object_uri(self, object_key: str) -> str:
        return self.path_template.format(
            bucket=self.bucket,
            key=object_key.lstrip("/"),
            profile_id=self.profile_id,
        )

    def validate_object_key(self, object_key: str) -> str:
        key = str(object_key or "").strip()
        if not key:
            raise ValueError("object_key must not be blank")
        if key.startswith("/") or "\\" in key:
            raise ValueError(f"invalid object_key: {object_key}")
        parts = [part for part in key.split("/") if part]
        if any(part == ".." for part in parts):
            raise ValueError(f"invalid object_key: {object_key}")
        if self.allowed_prefixes and not any(key.startswith(prefix) for prefix in self.allowed_prefixes):
            raise ValueError(f"object_key prefix is not allowed: {object_key}")
        return key


class StorageProfileRegistry:
    def __init__(
        self,
        profiles: Mapping[str, StorageProfile],
        default_profile_id: str,
        aliases: Mapping[str, str] | None = None,
    ):
        self.profiles = dict(profiles)
        self.aliases = {
            str(alias).strip(): str(target).strip()
            for alias, target in (aliases or {}).items()
            if str(alias).strip() and str(target).strip()
        }
        self.default_profile_id = str(default_profile_id or "").strip()
        self.default_profile_id = self._resolve_profile_id(self.default_profile_id)
        if self.default_profile_id not in self.profiles and self.profiles:
            self.default_profile_id = next(iter(self.profiles))

    def _resolve_profile_id(self, profile_id: str | None) -> str:
        resolved_id = (profile_id or getattr(self, "default_profile_id", "") or "").strip()
        seen: list[str] = []
        while resolved_id in self.aliases:
            if resolved_id in seen:
                chain = " -> ".join((*seen, resolved_id))
                raise KeyError(f"storage profile alias cycle: {chain}")
            seen.append(resolved_id)
            resolved_id = self.aliases[resolved_id]
        return resolved_id

    def get(self, profile_id: str | None = None) -> StorageProfile:
        resolved_id = self._resolve_profile_id(profile_id)
        if not resolved_id:
            raise KeyError("storage profile id is required")
        try:
            return self.profiles[resolved_id]
        except KeyError as exc:
            raise KeyError(f"unknown storage profile: {resolved_id}") from exc

    def default(self) -> StorageProfile:
        return self.get(self.default_profile_id)


def _profile_from_dict(profile_id: str, raw: Mapping[str, Any], env: Mapping[str, str]) -> StorageProfile:
    data = _resolve_env_refs(dict(raw), env)
    allowed_prefixes = data.get("allowed_prefixes", ())
    if isinstance(allowed_prefixes, str):
        allowed_prefixes = _csv_items(allowed_prefixes)
    cdn_auth_keys = data.get("cdn_auth_keys", ())
    if isinstance(cdn_auth_keys, str):
        cdn_auth_keys = _csv_items(cdn_auth_keys)
    elif not isinstance(cdn_auth_keys, (list, tuple)):
        cdn_auth_keys = ()
    cdn_auth_key = str(data.get("cdn_auth_key") or "")
    normalized_cdn_auth_keys = tuple(str(item).strip() for item in cdn_auth_keys if str(item).strip())
    if cdn_auth_key and cdn_auth_key not in normalized_cdn_auth_keys:
        normalized_cdn_auth_keys = (cdn_auth_key, *normalized_cdn_auth_keys)
    return StorageProfile(
        profile_id=str(data.get("profile_id") or data.get("id") or profile_id),
        bucket=str(data.get("bucket") or ""),
        endpoint=str(data.get("endpoint") or ""),
        public_endpoint=str(data.get("public_endpoint") or ""),
        cdn_endpoint=str(data.get("cdn_endpoint") or ""),
        access_key=str(data.get("access_key") or ""),
        secret_key=str(data.get("secret_key") or ""),
        region=str(data.get("region") or ""),
        signature_version=str(data.get("signature_version") or "s3v4"),
        addressing_style=str(data.get("addressing_style") or "auto"),
        url_mode=str(data.get("url_mode") or "s3_presign"),
        path_template=str(data.get("path_template") or "/{key}"),
        allowed_prefixes=tuple(str(item).strip() for item in allowed_prefixes if str(item).strip()),
        cdn_auth_type=str(data.get("cdn_auth_type") or "A"),
        cdn_auth_algorithm=str(data.get("cdn_auth_algorithm") or "md5"),
        cdn_auth_sign_param=str(data.get("cdn_auth_sign_param") or "sign"),
        cdn_auth_expires=int(data.get("cdn_auth_expires") or 86400),
        cdn_auth_time_format=str(data.get("cdn_auth_time_format") or "unix_decimal"),
        cdn_auth_scope=str(data.get("cdn_auth_scope") or "file_suffix"),
        cdn_auth_file_suffixes=_string_items(data.get("cdn_auth_file_suffixes"), default=("*",)),
        cdn_auth_uid=str(data.get("cdn_auth_uid") or "0"),
        cdn_auth_rand=str(data.get("cdn_auth_rand") or "0"),
        cdn_auth_key=cdn_auth_key,
        cdn_auth_keys=normalized_cdn_auth_keys,
    )


def _default_profiles_files() -> tuple[Path, ...]:
    client_root = Path(__file__).resolve().parents[2]
    return (
        client_root / "storage_profiles.jsonc",
        client_root / "storage_profiles.json",
    )


def _load_profiles_payload_from_file(env: Mapping[str, str]) -> Mapping[str, Any] | None:
    raw_path = _env_first(env, *PROFILE_FILE_ENV_NAMES)
    paths = (Path(raw_path),) if raw_path else _default_profiles_files()
    path = next((candidate for candidate in paths if candidate.is_file()), None)
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = _loads_jsonc(handle.read())
    if not isinstance(payload, Mapping):
        raise ValueError(f"storage profiles file must be a JSON object: {path}")
    return payload


def _profiles_from_payload(
    payload: Mapping[str, Any],
    env: Mapping[str, str],
    default_profile_id: str,
) -> tuple[dict[str, StorageProfile], str, dict[str, str]]:
    aliases: dict[str, str] = {}
    if "profiles" in payload:
        raw_profiles = payload.get("profiles") or {}
        default_profile_id = str(payload.get("default_profile_id") or default_profile_id)
        raw_aliases = payload.get("aliases") or {}
        if not isinstance(raw_aliases, Mapping):
            raise ValueError("storage profile aliases must be an object")
        aliases = {
            str(alias).strip(): str(target).strip()
            for alias, target in raw_aliases.items()
            if str(alias).strip() and str(target).strip()
        }
    else:
        raw_profiles = payload
    if not isinstance(raw_profiles, Mapping):
        raise ValueError("storage profiles JSON must be an object")
    profiles: dict[str, StorageProfile] = {}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"storage profile {profile_id!r} must be an object")
        profile = _profile_from_dict(str(profile_id), raw, env)
        profiles[profile.profile_id] = profile
    if not default_profile_id and profiles:
        default_profile_id = next(iter(profiles))
    return profiles, default_profile_id, aliases


def load_storage_profiles(env: Mapping[str, str] | None = None) -> StorageProfileRegistry:
    env = env or os.environ
    profiles: dict[str, StorageProfile] = {}
    default_profile_id = _env_first(env, *DEFAULT_PROFILE_ENV_NAMES)
    aliases: dict[str, str] = {}

    file_payload = _load_profiles_payload_from_file(env)
    if file_payload:
        profiles, default_profile_id, aliases = _profiles_from_payload(file_payload, env, default_profile_id)

    raw_json = _env_first(env, *PROFILE_JSON_ENV_NAMES)
    if raw_json:
        payload = json.loads(raw_json)
        if not isinstance(payload, Mapping):
            raise ValueError("storage profiles JSON must be an object")
        profiles, default_profile_id, aliases = _profiles_from_payload(payload, env, default_profile_id)

    if not profiles:
        raise ValueError("no storage profiles configured; configure client/storage_profiles.jsonc")

    return StorageProfileRegistry(profiles, default_profile_id, aliases)
