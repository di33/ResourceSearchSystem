"""Client object-storage upload command.

This command only uploads local resources/previews to object storage and stores
the manifest in the client DB. It does not submit processing jobs.
"""

from __future__ import annotations

from ObjectStorageUpload.resource_manifest import (  # noqa: F401
    build_manifests_from_cache,
    upload_entity_objects,
    write_manifest_records,
)
from ObjectStorageUpload.upload_resources import main  # noqa: F401


if __name__ == "__main__":
    raise SystemExit(main())
