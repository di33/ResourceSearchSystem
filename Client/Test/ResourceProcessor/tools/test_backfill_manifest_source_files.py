from ResourceProcessor.tools.backfill_manifest_source_files import build_source_files


def test_build_source_files_restores_relative_paths_and_primary(tmp_path):
    root = tmp_path / "asset"
    rows = [
        {
            "file_path": str(root / "run" / "run_01.png"),
            "file_name": "run_01.png",
            "file_size": 10,
            "file_format": ".PNG",
            "content_md5": "a",
            "is_primary": 1,
        },
        {
            "file_path": str(root / "run" / "run_02.png"),
            "file_name": "run_02.png",
            "file_size": 11,
            "file_format": "png",
            "content_md5": "b",
            "is_primary": 0,
        },
    ]

    assert build_source_files(str(root), rows) == [
        {
            "file_name": "run_01.png",
            "file_format": "png",
            "file_size": 10,
            "checksum": "a",
            "path_in_package": "run/run_01.png",
            "is_primary": True,
        },
        {
            "file_name": "run_02.png",
            "file_format": "png",
            "file_size": 11,
            "checksum": "b",
            "path_in_package": "run/run_02.png",
            "is_primary": False,
        },
    ]


def test_build_source_files_disambiguates_duplicate_member_names(tmp_path):
    rows = [
        {"file_path": "a/same.png", "file_name": "same.png", "file_size": 1, "file_format": "png", "content_md5": "a", "is_primary": 0},
        {"file_path": "b/same.png", "file_name": "same.png", "file_size": 1, "file_format": "png", "content_md5": "b", "is_primary": 0},
    ]

    files = build_source_files("", rows)

    assert [item["path_in_package"] for item in files] == ["same.png", "same_1.png"]
    assert [item["is_primary"] for item in files] == [True, False]
