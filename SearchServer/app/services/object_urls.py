"""Object URL generation from storage profiles."""

from __future__ import annotations

from app.services.storage_profiles import StorageProfile, load_storage_profiles
from resource_contracts.object_urls import generate_cdn_download_url


class ObjectUrlGenerator:
    """Generate object download URLs from storage profiles."""

    def __init__(self):
        self.profiles = load_storage_profiles()

    def _profile(self, storage_profile_id: str = "") -> StorageProfile:
        return self.profiles.get(storage_profile_id or None)

    def generate_download_url(
        self,
        key: str,
        expires: int | None = None,
        storage_profile_id: str = "",
    ) -> str:
        profile = self._profile(storage_profile_id)
        key = profile.validate_object_key(key)
        url_mode = (profile.url_mode or "cdn_unsigned").strip().lower()
        if profile.cdn_endpoint and url_mode in {"cdn_unsigned", "cdn_type_a"}:
            return generate_cdn_download_url(profile, key, expires=expires)

        raise RuntimeError(
            f"SearchServer requires a CDN URL mode for storage profile {profile.profile_id}: {url_mode}"
        )
