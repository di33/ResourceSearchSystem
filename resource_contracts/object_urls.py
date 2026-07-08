from __future__ import annotations

import hashlib
import time
from urllib.parse import quote, urlencode


def cdn_auth_type_a_signature(uri: str, timestamp: int, rand: str, uid: str, key: str) -> str:
    return hashlib.md5(f"{uri}-{timestamp}-{rand}-{uid}-{key}".encode("utf-8")).hexdigest()


def cdn_auth_matches_suffix_scope(key: str, profile) -> bool:
    scope = (getattr(profile, "cdn_auth_scope", "") or "file_suffix").strip().lower()
    if scope in {"all", "global"}:
        return True
    if scope not in {"file_suffix", "suffix"}:
        raise ValueError(f"Unsupported CDN auth scope: {getattr(profile, 'cdn_auth_scope', '')}")
    suffixes = {
        str(item).strip().lower().lstrip(".")
        for item in (getattr(profile, "cdn_auth_file_suffixes", ()) or ())
        if str(item).strip()
    }
    if not suffixes or suffixes.intersection({"*", "all"}):
        return True
    file_name = key.rsplit("/", 1)[-1]
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return suffix in suffixes


def cdn_auth_timestamp(profile, ttl: int) -> int:
    time_format = (getattr(profile, "cdn_auth_time_format", "") or "unix_decimal").strip().lower()
    if time_format not in {"unix", "unix_timestamp", "unix_decimal", "decimal"}:
        raise ValueError(f"Unsupported CDN auth time format: {getattr(profile, 'cdn_auth_time_format', '')}")
    return int(time.time()) + ttl


def generate_cdn_download_url(profile, key: str, expires: int | None = None) -> str:
    cdn_endpoint = str(getattr(profile, "cdn_endpoint", "") or "")
    if not cdn_endpoint:
        raise RuntimeError(f"CDN endpoint is required for storage profile {getattr(profile, 'profile_id', '')}")
    uri = profile.object_uri(quote(key, safe="/"))
    if not uri.startswith("/"):
        uri = f"/{uri}"
    url = f"{cdn_endpoint.rstrip('/')}{uri}"

    url_mode = (getattr(profile, "url_mode", "") or "cdn_unsigned").strip().lower()
    if url_mode == "cdn_unsigned":
        return url
    if url_mode != "cdn_type_a":
        raise RuntimeError(f"Unsupported CDN URL mode for storage profile {getattr(profile, 'profile_id', '')}: {url_mode}")
    if not cdn_auth_matches_suffix_scope(key, profile):
        return url
    if (getattr(profile, "cdn_auth_type", "") or "A").strip().upper() != "A":
        raise ValueError(f"Unsupported CDN auth type: {getattr(profile, 'cdn_auth_type', '')}")
    if (getattr(profile, "cdn_auth_algorithm", "") or "md5").strip().lower() != "md5":
        raise ValueError(f"Unsupported CDN auth algorithm: {getattr(profile, 'cdn_auth_algorithm', '')}")

    auth_keys = getattr(profile, "cdn_auth_keys", ()) or ()
    auth_key = next(iter(auth_keys), "") or getattr(profile, "cdn_auth_key", "")
    if not auth_key:
        raise RuntimeError(f"CDN auth key is required for storage profile {getattr(profile, 'profile_id', '')}")
    ttl = expires or int(getattr(profile, "cdn_auth_expires", 0) or 86400)
    timestamp = cdn_auth_timestamp(profile, ttl)
    rand = getattr(profile, "cdn_auth_rand", "") or "0"
    uid = getattr(profile, "cdn_auth_uid", "") or "0"
    signature = cdn_auth_type_a_signature(uri, timestamp, rand, uid, auth_key)
    token = f"{timestamp}-{rand}-{uid}-{signature}"
    query = urlencode({getattr(profile, "cdn_auth_sign_param", "") or "sign": token})
    return f"{url}?{query}"
