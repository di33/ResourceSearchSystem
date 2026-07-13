# ResourceUpload 客户端命令指南

本文只介绍这 5 个客户端命令：

- `sync_pipeline_from_crawler_state`
- `upload_objects_to_storage`
- `generate_previews`
- `generate_descriptions`
- `upload_resources`

每个新 PowerShell 窗口先执行一次初始化：

```powershell
cd G:\ResourceUpload; $env:PYTHONPATH = "G:\ResourceUpload\client\Scripts;G:\ResourceUpload\Tools"; $py = ".\.venv\Scripts\python.exe"
```

下面示例都省略默认参数。命令默认读取 `Tools\.env`、`Tools\.env.local`、`client\.env`、`client\.env.local`。

## 1. sync_pipeline_from_crawler_state

从 ResourceCrawler 的 `crawler_state.db` 同步到本地 `pipeline.db`。

示例：

```powershell
& $py -m ResourceProcessor.tools.sync_pipeline_from_crawler_state
```

dry-run 看差异：

```powershell
& $py -m ResourceProcessor.tools.sync_pipeline_from_crawler_state --dry-run
```

全量重建：

```powershell
& $py -m ResourceProcessor.tools.sync_pipeline_from_crawler_state --clear-first
```

参数：

- `--crawler-state-db`：ResourceCrawler 状态库。默认 `G:\ResourceCrawler\data\crawler_state.db`。
- `--crawler-output`：ResourceCrawler 输出目录。默认 `K:\ResourceCrawler\output`。
- `--db-path`：目标 pipeline SQLite。默认 `G:\ResourceUpload\data\databases\pipeline.db`。
- `--dry-run`：只统计差异，不写库、不删文件。默认关闭。
- `--clear-first`：同步前清空当前 pipeline 表。默认关闭。
- `--replace-db-file`：配合 `--clear-first`，直接删除旧 SQLite 文件后重建。默认关闭。
- `--no-backup`：执行前不备份目标数据库。默认关闭，即默认会备份。
- `--keep-preview-files`：只删 `resource_preview` 记录，不删磁盘预览文件。默认关闭。
- `--no-object-delete-jobs`：删除本地资源时不写入对象存储删除队列。默认关闭，即默认会写入删除队列。
- `--preview-dir`：允许清理预览文件的目录，可重复传。默认自动推断常见 `previews` 目录。
- `--commit-every`：每处理 N 条新增或变更资源提交一次。默认 `1000`。
- `--asset-batch-size`：同步 `asset_index` 的批大小。默认 `10000`。

## 2. upload_objects_to_storage

把本地资源文件/预览上传到对象存储，并把对象引用 manifest 保存到 `pipeline.db`。它负责对象桶副作用，包括上传新对象、清理旧对象，并在对象引用变化后刷新资源总指纹；不提交加工服务器。推荐在远程生成预览前先跑一次源对象上传，生成或刷新预览后再跑一次补齐预览对象。

先 dry-run：

```powershell
& $py -m ResourceProcessor.upload_objects_to_storage --no-previews --missing-manifest-only --dry-run --limit 5
```

正式上传源对象：

```powershell
& $py -m ResourceProcessor.upload_objects_to_storage --no-previews --missing-manifest-only
```

生成或刷新预览后，刷新对象 manifest 和预览对象：

```powershell
& $py -m ResourceProcessor.upload_objects_to_storage
```

只补传指定资源类型：

```powershell
& $py -m ResourceProcessor.upload_objects_to_storage --no-previews --missing-manifest-only --resource-types "atlas,tileset,spine_skeleton"
```

参数：

- `--db-path`：本地 pipeline SQLite。默认 `G:\ResourceUpload\data\databases\pipeline.db`。
- `--limit`：最多处理多少个资源。默认不限制。
- `--resource-type`：只处理一个资源类型。默认空，即不按单个类型过滤。
- `--resource-types`：只处理多个资源类型，支持逗号分隔或重复传入。默认空，即不按多个类型过滤。
- `--source-filter`：只处理指定来源站点。默认空，即不过滤来源。
- `--resume`：通用断点续跑参数。默认关闭；该命令在未使用 `--force` 时本身会复用或跳过已有 manifest。
- `--client-id`：客户端命名空间。默认读取 `CLIENT_ID`，未配置则 `client`。
- `--storage-profile-id`：对象存储 profile ID。默认读取 `STORAGE_PROFILE_ID` / `OBJECT_STORAGE_PROFILE_ID`，未配置则使用 `client\storage_profiles.jsonc` 的默认 profile。
- `--key-prefix`：对象 key 根前缀。默认空。
- `--no-previews`：不上传本地已有预览。默认关闭，即默认会上传已有预览。
- `--no-descriptions`：不携带本地已有描述。默认关闭，即默认会携带已有描述；没有本地描述时不会输出 `description`。
- `--manifest-out`：导出 JSONL manifest 文件。默认空；真实上传未传则只写 DB，dry-run 未传则输出到 stdout。
- `--dry-run`：只构造计划 manifest，不上传对象、不写 DB。默认关闭。
- `--force`：强制重新上传匹配资源；必须配合 `--resource-type` 或 `--resource-types`。默认关闭。
- `--process-states`：只上传指定任务状态，支持逗号分隔或重复传入。默认空，即不过滤状态。
- `--min-task-id`：只处理 id 大于等于该值的任务。默认不限制。
- `--max-task-id`：只处理 id 小于等于该值的任务。默认不限制。
- `--preview-created-after`：只处理该时间之后生成过预览的任务。默认空。
- `--missing-manifest-only`：只处理尚未保存 uploaded manifest 的任务。默认关闭。
- `--defer-replaced-object-cleanup`：替换 manifest 后把旧对象写入删除队列，稍后统一清理。默认关闭。
- `--workers`：并发上传 worker 数。默认读取 `OBJECT_STORAGE_UPLOAD_WORKERS`，未配置则 `8`。

说明：`pack` 任务只上传包对象并保存 manifest，不会提交加工服务器。

## 3. generate_previews

生成预览并写回 `pipeline.db`。默认使用 renderer 模式，因此需要先有对象存储 manifest。

断点生成：

```powershell
& $py -m ResourceProcessor.generate_previews --skip-missing-object-manifest
```

强制重建某类资源：

```powershell
& $py -m ResourceProcessor.generate_previews --resource-type single_image --force --skip-missing-object-manifest
```

只处理指定 task：

```powershell
& $py -m ResourceProcessor.generate_previews --task-id 12345
```

参数：

- `--db-path`：本地 pipeline SQLite。默认 `G:\ResourceUpload\data\databases\pipeline.db`。
- `--limit`：最多处理多少个资源。默认不限制。
- `--resource-type`：只处理指定资源类型。默认空，即处理所有可生成预览的非 `pack` 资源。
- `--source-filter`：只处理指定来源站点。默认空，即不过滤来源。
- `--resume`：通用断点续跑参数。默认关闭；当前预览命令默认也会跳过已达到 `preview_ready` 的任务。
- `--work-dir`：工作目录。默认 `G:\ResourceUpload\data`，预览写到 `<work-dir>\previews\<resource_type>\`。
- `--force`：强制重新生成匹配资源预览，成功后清除旧预览记录和旧文件。默认关闭。
- `--task-id`：只处理指定 task id，可重复传。默认空。
- `--min-task-id`：只处理 id 大于等于该值的 task。默认不限制。
- `--max-task-id`：只处理 id 小于等于该值的 task。默认不限制。
- `--preview-mode`：预览方式。默认 `renderer`；可选 `local`。
- `--preview-renderer`：preview-renderer 地址。默认读取 `PREVIEW_RENDERER_URL`，未配置则 `http://localhost:8200`。
- `--client-id`：renderer 请求的 `X-Client-Id`。默认读取 `CLIENT_ID`，未配置则 `client`。
- `--api-key`：preview-renderer API key。默认读取 `PR_PREVIEW_RENDERER_API_KEY` / `PR_API_KEY`，未配置则空。
- `--phase`：兼容旧 worker 的刷新阶段参数。默认 `non-pack`；当前命令始终跳过 `pack` 预览。
- `--marker`：只处理 marker 时间之后没有新预览的任务。默认空。
- `--worker-count`：并行 worker 总数，按 task id 取模分片。默认 `1`。
- `--worker-index`：当前 worker 下标。默认 `0`。
- `--progress-every`：每处理 N 个任务打印一次进度。默认 `25`。
- `--status-file`：状态日志文件。默认空。
- `--skip-missing-object-manifest`：renderer 模式下跳过没有对象存储 manifest 的任务。默认关闭。

## 4. generate_descriptions

读取预览结果，生成描述并写回 `pipeline.db`。

断点生成：

```powershell
& $py -m ResourceProcessor.generate_descriptions --resume
```

重试失败描述：

```powershell
& $py -m ResourceProcessor.generate_descriptions --retry-failed
```

强制刷新某类资源描述：

```powershell
& $py -m ResourceProcessor.generate_descriptions --resource-type single_image --force
```

参数：

- `--db-path`：本地 pipeline SQLite。默认 `G:\ResourceUpload\data\databases\pipeline.db`。
- `--limit`：最多处理多少个资源。默认不限制。
- `--resource-type`：只处理指定资源类型。默认空，即不过滤类型。
- `--source-filter`：只处理指定来源站点。默认空，即不过滤来源。
- `--resume`：跳过已有描述的资源，并把它们标记为 `description_ready`。默认关闭。
- `--llm-provider`：非音频资源使用的 LLM provider。默认读取 `CLIENT_LLM_PROVIDER`，未配置则 `mock`。
- `--audio-llm-provider`：音频资源使用的 LLM provider。默认读取 `AUDIO_LLM_PROVIDER`，未配置则跳过音频。
- `--retry-failed`：只重试描述生成失败的任务。默认关闭。
- `--force`：强制刷新匹配资源描述；必须配合 `--resource-type`，且不能和 `--retry-failed` 同用。默认关闭。
- `--max-retries`：失败任务最多重试次数。默认 `3`。
- `--concurrency`：并发请求数。默认：Codex provider 读取 `CODEX_CONCURRENCY`，未配置则 `1`；其他 provider 读取 `DESCRIPTION_CONCURRENCY`，未配置则 `5`。

常用环境变量：`KSPMAS_LLM_TIMEOUT` 控制 Ksyun LLM 请求超时，未配置时由 provider 默认值决定；大批量视觉描述常用 `180`。

## 5. upload_resources

读取已上传对象 manifest，按当前总指纹判断是否需要提交，临时生成最终加工 manifest，并提交到 resource-processing-server。该命令不上传对象、不清理旧对象；对象桶更新由 `upload_objects_to_storage` 完成。

先 dry-run：

```powershell
& $py -m ResourceProcessor.upload_resources --dry-run --limit 5
```

正式提交。默认先对账已有 queued job，再以 32 并发快速写入加工服务器的持久队列，完成一次批量状态检查后立即退出；后续再次运行时会继续对账，只有服务端确认完成后，本地状态才会更新为 `submitted`：

```powershell
& $py -m ResourceProcessor.upload_resources
```

需要持续等待并批量对账，直到本地没有 queued/submitting 任务：

```powershell
& $py -m ResourceProcessor.upload_resources --wait-all
```

强制重提指定资源类型：

```powershell
& $py -m ResourceProcessor.upload_resources --force --resource-types "atlas,tileset,tiled_map,animation_sequence,spine_skeleton,pack"
```

临时使用旧的逐条同步等待方式调试：

```powershell
& $py -m ResourceProcessor.upload_resources --wait --limit 5
```

参数：

- `--db-path`：本地 pipeline SQLite。默认 `G:\ResourceUpload\data\databases\pipeline.db`。
- `--limit`：最多处理多少个资源。默认不限制。
- `--resource-type`：只提交一个资源类型。默认空，即不按单个类型过滤。
- `--resource-types`：只提交多个资源类型，支持逗号分隔或重复传入。默认空，即不按多个类型过滤。
- `--source-filter`：只提交指定来源站点。默认空，即不过滤来源。
- `--resume`：通用断点续跑参数。默认关闭；该命令按总指纹自动跳过已提交资源。
- `--processing-server`：资源加工服务器地址。默认读取 `RP_PROCESSING_SERVER_URL`，未配置则 `http://localhost:8100`。
- `--client-id`：客户端命名空间。默认读取 `CLIENT_ID`，未配置则 `client`。
- `--api-key`：资源加工服务器 API key。默认读取 `RP_PROCESSING_SERVER_API_KEY` / `RP_API_KEY`，未配置则空。
- `--manifest-out`：导出 JSONL manifest 文件。默认空，即不额外导出文件。
- `--dry-run`：只构造待提交 manifest，不提交加工服务器。默认关闭。
- `--no-wait`：任务持久化入队后立即退出。这已经是默认行为，参数仅为兼容旧命令保留。
- `--wait-all`：全部任务入队后持续批量对账，直到本地没有 queued/submitting 任务。默认关闭。
- `--wait`：逐条提交并等待单个 job 完成，仅用于小批量调试。默认关闭。
- `--poll-interval`：批量对账或逐条等待的间隔秒数。默认 `0.2`。
- `--wait-timeout`：等待加工完成的超时秒数。默认读取 `RP_PROCESSING_JOB_TIMEOUT`，未配置则 `3600`；传 `0` 表示不超时。
- `--concurrency`：并发提交 worker 数。默认读取 `RP_UPLOAD_RESOURCES_CONCURRENCY`，未配置则 `32`。
- `--force`：强制重新提交匹配资源；必须配合 `--resource-type` 或 `--resource-types`。默认关闭。

说明：`upload_resources` 比较当前资源总指纹和最近一次成功提交的 `committed_fingerprint`；资源总指纹包含已上传对象引用，因此 `upload_objects_to_storage` 刷新对象 key 后会触发重新提交。`object_fingerprint` 只用于 `upload_objects_to_storage` 判断桶对象是否需要重传，`upload_resources` 不使用它。命令中断后直接重新运行即可：客户端会先对账 `queued/submitting`，已完成任务转成 `submitted`，失败或服务端找不到的任务在同一次运行中重新入队。
