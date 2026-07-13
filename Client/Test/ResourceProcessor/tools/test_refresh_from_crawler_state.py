from ResourceProcessor.tools import refresh_from_crawler_state as refresh


def test_refresh_builds_default_step_order_without_second_object_upload():
    args = refresh._parser().parse_args(
        [
            "--crawler-state-db",
            "crawler.db",
            "--crawler-output",
            "crawler-output",
            "--db-path",
            "pipeline.db",
            "--work-dir",
            "work",
            "--client-id",
            "resource-crawler",
            "--processing-server",
            "http://processor",
        ]
    )

    steps = refresh.build_steps(args)

    assert [step.name for step in steps] == [
        "sync_pipeline_from_crawler_state",
        "upload_objects_to_storage",
        "flush_object_delete_jobs",
        "generate_previews",
        "generate_descriptions",
        "upload_resources",
    ]
    assert sum(step.module == "ResourceProcessor.upload_objects_to_storage" for step in steps) == 1
    preview_step = next(step for step in steps if step.name == "generate_previews")
    assert "--skip-missing-object-manifest" in preview_step.args


def test_refresh_forwards_optional_filters_and_preview_delete_flush():
    args = refresh._parser().parse_args(
        [
            "--db-path",
            "pipeline.db",
            "--client-id",
            "resource-crawler",
            "--limit",
            "10",
            "--resource-type",
            "single_image",
            "--source-filter",
            "itch",
            "--missing-manifest-only",
            "--flush-object-deletes-after-previews",
        ]
    )

    steps = refresh.build_steps(args)

    assert [step.name for step in steps].count("flush_object_delete_jobs") == 1
    assert [step.name for step in steps].count("flush_object_delete_jobs_after_previews") == 1
    upload_step = next(step for step in steps if step.name == "upload_objects_to_storage")
    assert "--missing-manifest-only" in upload_step.args
    for step_name in ("upload_objects_to_storage", "generate_previews", "generate_descriptions", "upload_resources"):
        step = next(step for step in steps if step.name == step_name)
        assert step.args[step.args.index("--limit") + 1] == "10"
        assert step.args[step.args.index("--resource-type") + 1] == "single_image"
        assert step.args[step.args.index("--source-filter") + 1] == "itch"
