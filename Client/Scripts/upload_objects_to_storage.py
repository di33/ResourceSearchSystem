from __future__ import annotations

from client_env import init_client_env


init_client_env()

from ResourceProcessor.upload_objects_to_storage import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
