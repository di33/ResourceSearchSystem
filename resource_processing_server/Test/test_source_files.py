from __future__ import annotations

import zipfile

import pytest

from resource_contracts.source_files import resolve_local_source_files, safe_zip_member_name


def test_resolve_local_source_files_extracts_only_requested_members(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    package = source_dir / "source.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("assets/declared.txt", b"declared")
        archive.writestr("assets/unused.txt", b"unused")

    paths = resolve_local_source_files(
        package,
        [{"file_name": "declared.txt", "path_in_package": "assets/declared.txt"}],
        source_dir,
    )

    assert [path.read_bytes() for path in paths] == [b"declared"]
    assert not (source_dir / "extracted" / "assets" / "unused.txt").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "/absolute.txt", "C:/evil.txt"])
def test_safe_zip_member_name_rejects_unsafe_paths(name):
    with pytest.raises(RuntimeError, match="unsafe package path"):
        safe_zip_member_name(name)


def test_resolve_local_source_files_rejects_too_many_archive_members(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    package = source_dir / "source.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.txt", b"a")
        archive.writestr("b.txt", b"b")

    with pytest.raises(RuntimeError, match="too many package members"):
        resolve_local_source_files(
            package,
            [{"file_name": "a.txt"}],
            source_dir,
            max_zip_members=1,
        )


def test_resolve_local_source_files_rejects_extract_size_limit(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    package = source_dir / "source.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("large.txt", b"1234567890")

    with pytest.raises(RuntimeError, match="zip extracted size exceeds limit"):
        resolve_local_source_files(
            package,
            [{"file_name": "large.txt"}],
            source_dir,
            max_zip_extract_bytes=5,
        )
