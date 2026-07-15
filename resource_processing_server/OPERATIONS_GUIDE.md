# ResourceUpload 资源加工服务器操作指南

除特别说明外，命令都在仓库根目录执行。Windows 使用 PowerShell，Linux 使用 Bash。下面的命令都尽量写成单行，复制整行即可运行。

Windows 进入仓库根目录：

```powershell
cd G:\ResourceUpload
```

Linux 进入仓库根目录：

```bash
cd /path/to/ResourceUpload
```

Windows 下不要直接使用裸 `python`：它可能命中 WindowsApps 的 Python shim。下面的 PowerShell 示例都显式使用仓库里的 `.\.venv\Scripts\python.exe`。

## 1. 准备

安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r resource_processing_server\requirements.txt
```

客户端 ResourceProcessor 公开配置在 `Tools/.env`，客户端私有 LLM Key 在 `Client/.env.local`。服务端描述生成使用 `resource_processing_server/.env` 中的 `RP_LLM_PROVIDER` / `RP_LLM_MODEL`，对应私有 LLM Key 放在 `resource_processing_server/.env.local`。

资源加工服务器自身配置放在 `resource_processing_server/.env` 和 `resource_processing_server/.env.local`。对象存储 profile 放在 `resource_processing_server/storage_profiles.jsonc`，其中包含 bucket、endpoint、region、CDN、URL 模式等稳定配置；密钥通过环境变量注入。公开项可放 `.env`，密钥只放 `.env.local`。加工服务器需要下载源对象，所以它使用的 profile 必须配置 `endpoint`。

最小配置示例：

```env
# resource_processing_server/.env
RP_SEARCH_SERVER_URL=http://localhost:8000
RP_DATABASE_URL=postgresql://resource_processor:<从.env.local读取的强密码>@localhost:5433/resource_processing
RP_GENERATED_PREVIEW_PREFIX=
RP_WORK_DIR=G:\ResourceUpload\data\resource_processing_server
RP_SNAPSHOT_DB_PATH=G:\ResourceUpload\data\resource_processing_server\snapshots.db
RP_JOB_WORKER_CONCURRENCY=32
RP_LLM_PROVIDER=ksyun
RP_PROCESS_INLINE=false
RP_DESCRIPTION_BATCH_ENABLED=true
RP_DESCRIPTION_BATCH_MIN_SIZE=20
RP_DESCRIPTION_BATCH_MAX_SIZE=200
RP_DESCRIPTION_BATCH_MAX_WAIT_SECONDS=1

# resource_processing_server/.env.local
OBJECT_STORAGE_ACCESS_KEY=minioadmin
OBJECT_STORAGE_SECRET_KEY=minioadmin
OBJECT_STORAGE_CDN_AUTH_KEY_PRIMARY=你的 CDN 主鉴权密钥
OBJECT_STORAGE_CDN_AUTH_KEY_SECONDARY=你的 CDN 备鉴权密钥
RP_SEARCH_SERVER_API_KEY=你的SearchServer API Key
KSPMAS_API_KEY=你的金山云API Key
```

如果 SearchServer 使用 Bearer Token：

```env
RP_SEARCH_SERVER_BEARER_TOKEN=你的BearerToken
```

## 2. 启动

### 2.1 本机 Python 启动

开发模式启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn resource_processing_server.app.main:app --reload --host 0.0.0.0 --port 8100
```

生产模式启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn resource_processing_server.app.main:app --host 0.0.0.0 --port 8100
```

健康检查：

```powershell
$server = "http://localhost:8100"; Invoke-RestMethod -Uri "$server/health" -Method Get
```

### 2.2 Docker 独立部署

资源加工服务器独立部署，不并入 SearchServer 的 compose。它通过 `RP_SEARCH_SERVER_URL` 访问 SearchServer，因此两台服务器可以在不同物理机上。加工任务、处理快照和删除标记保存在加工服务器自己的 Postgres；Docker Compose 默认使用 `postgres_data` named volume，宿主机调试端口为 `5433`，不会占用 SearchServer Postgres 的 `5432`。

首次从旧版本升级时，服务会把 `RP_SNAPSHOT_DB_PATH` 指向的 SQLite 快照和 job 历史一次性导入 Postgres。导入完成后运行时不再写 `snapshots.db`，旧文件保留为迁移归档。不要对运行中的 Postgres volume 执行 `docker compose down -v`，除非明确要清空加工任务和快照。

`preview-renderer` 独立启动。加工服务器默认通过 `RP_PREVIEW_RENDERER_URL=http://host.docker.internal:8200` 调用宿主机上已经启动的 renderer；Windows client 需要使用同一套容器工具链时，也访问 `http://localhost:8200`。renderer 不读取对象存储密钥，也不自己生成 URL；调用方负责生成临时读取 URL 并在请求里传 `source_object_url`。

本机开发时，如果 SearchServer 跑在宿主机 `8000` 端口，默认配置可直接启动：

```powershell
cd G:\ResourceUpload\resource_processing_server; docker compose up -d --build
```

查看加工服务器 Postgres 状态：

```powershell
cd G:\ResourceUpload\resource_processing_server; docker compose exec postgres psql -U resource_processor -d resource_processing -c "select state, count(*) from processing_job group by state order by state;"
```

如果需要加工服务器调用统一预览服务，先单独启动 preview-renderer：

```powershell
cd G:\ResourceUpload\preview_renderer; docker compose up -d --build
```

如果 SearchServer 在另一台机器，先配置搜索服务器地址：

```powershell
cd G:\ResourceUpload\resource_processing_server; $env:RP_SEARCH_SERVER_URL="http://192.168.1.20:8000"; docker compose up -d --build
```

加工服务器对外监听地址和端口也可以配置：

```powershell
$env:RP_BIND_HOST="0.0.0.0"
$env:RP_PORT="8100"
```

需要 Blender 时再打开可选镜像层，默认不安装以避免镜像过重：

```powershell
cd G:\ResourceUpload\resource_processing_server; $env:INSTALL_BLENDER="true"; docker compose build
```

如果部署机器不希望在构建镜像时重新下载 ffmpeg、Chromium、Node、字体、Python wheels、npm 包等依赖，可以先预取三层 vendor。apt vendor 不依赖 Docker，会逐个下载 `.deb` 并在完整成功后写入 `manifest.json`：

```powershell
cd G:\ResourceUpload
.\resource_processing_server\docker\vendor\fetch_apt_vendor.ps1
.\resource_processing_server\docker\vendor\fetch_pip_vendor.ps1
.\resource_processing_server\docker\vendor\fetch_npm_vendor.ps1
```

需要 Blender 时：

```powershell
cd G:\ResourceUpload
.\resource_processing_server\docker\vendor\fetch_apt_vendor.ps1 -IncludeBlender
```

预取结果会写入 `resource_processing_server\docker\vendor\apt`，部署时把该目录一起带上。离线部署建议强制使用 vendor 包，目录为空或包不完整时直接构建失败：

```powershell
cd G:\ResourceUpload\resource_processing_server
$env:RP_APT_VENDOR_MODE="required"
docker compose build
```

三层 vendor 的输出目录分别是：

- `resource_processing_server\docker\vendor\apt`
- `resource_processing_server\docker\vendor\pip`
- `resource_processing_server\docker\vendor\npm`

Dockerfile 只有在对应目录存在 `manifest.json` 时才使用 vendor；如果下载中断导致 manifest 缺失，构建会回退到在线安装，或者在 `RP_APT_VENDOR_MODE=required` 时直接失败。

Spine 预览工具已放在 `Tools\spine_preview`。其中 `spine-webgl-3.8.js` 是渲染器运行时文件，会随 Docker 镜像复制；Node 依赖由镜像构建时按 `package-lock.json` 安装；如果 `vendor\npm` 已准备好，则使用本地 npm cache 离线安装。

Docker 模式仍然读取 `resource_processing_server/.env` 和 `.env.local`。最关键的跨机器配置是：

```env
RP_SEARCH_SERVER_URL=http://搜索服务器IP或域名:8000
RP_SEARCH_SERVER_API_KEY=你的SearchServer API Key
```

如果临时想让加工服务器回退到进程内本地生成预览，可以覆盖：

```powershell
$env:RP_PREVIEW_RENDERER_URL=""
```

client 侧直接调用 preview-renderer 的示例：

```powershell
cd G:\ResourceUpload
.\.venv\Scripts\python.exe .\client\Scripts\render_previews_via_renderer.py --manifest .\data\manifests\sample.jsonl --preview-renderer http://localhost:8200 --client-id resource-crawler
```

未传 `--manifest` 时，该脚本会从本地 pipeline DB 的对象存储 manifest 表读取，适合在资源对象已经上传后用同一套 Docker 工具链单独生成/检查预览。脚本会把 renderer 返回的 zip 或 primary 二进制保存到本地，默认根目录为 `data/previews`，实际写入 `<resource_type>/<client_resource_id>_<preview_name>`，例如 `single_image/asset_001_primary.webp`；可用 `--output-dir` 覆盖，只需要主预览时可加 `--primary-only`。

加工服务器对象存储 profile 使用仓库里的 `resource_processing_server/storage_profiles.jsonc`，容器会以只读方式挂载该文件。preview-renderer 不挂载 profile，也不需要 AK/SK 或 CDN 鉴权密钥。

## 3. 提交加工任务

资源加工服务器只接收 `storage_profile_id + object_key` manifest，不接收原文件或预览二进制上传。上传对象存储由客户端或资源生产方完成。

推荐对象 key 结构：

- 原文件：`{client_id}/files/{client_resource_id}/{relative_path}`
- 预览：`{client_id}/previews/{client_resource_id}/{preview_name}`
- 多预览命名：`primary.{ext}`、`gallery-001.{ext}`、`gallery-002.{ext}`
- 包文件上传为 `{client_id}/files/{package_id}/source.zip`，zip 内保留成员文件相对路径。包本身不提交加工服务器；需要下载包的资源在 manifest 里带 `package_object`。

`RP_GENERATED_PREVIEW_PREFIX` 只是可选根前缀；默认空时，加工服务器生成的预览也写入同一套 `{client_id}/previews/...` 结构，来源由 `origin=generated` 元数据区分。

单资源提交示例：

```powershell
$server = "http://localhost:8100"; $body = @{ client_resource_id = "asset-1"; resource_type = "single_image"; source_object = @{ storage_profile_id = "default"; object_key = "resource-crawler/files/asset-1/asset-1.png"; file_name = "asset-1.png"; file_format = "png" }; source_files = @(@{ file_name = "asset-1.png"; file_format = "png"; is_primary = $true }); package_object = @{ storage_profile_id = "default"; object_key = "resource-crawler/files/pack-1/source.zip" }; client_metadata = @{ title = "asset-1" } } | ConvertTo-Json -Depth 20; Invoke-RestMethod -Uri "$server/processing-jobs" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" } -ContentType "application/json" -Body $body
```

带客户端已有预览：

```json
{
  "provided_previews": [
    {
      "role": "primary",
      "storage_profile_id": "default",
      "object_key": "resource-crawler/previews/asset-1/primary.webp",
      "width": 512,
      "height": 512,
      "origin": "provided"
    }
  ]
}
```

带客户端已有描述。请求中不传 `full_description`：

```json
{
  "provided_description": {
    "main_content": "像素风钥匙道具图标，适合背包、掉落物和解谜场景。",
    "detail_content": "图标轮廓清晰，颜色对比明显，可作为游戏内可拾取钥匙或门锁提示素材。",
    "prompt_version": "client-provided-v1",
    "source": "client"
  }
}
```

服务端固定规则：

- 有 `provided_previews`：校验并复用；校验失败则任务失败。
- 没有 `provided_previews`：生成预览。
- 有 `provided_description`：复用并跳过描述生成。
- 没有 `provided_description`：进入描述生成队列。

查询任务：

```powershell
$server = "http://localhost:8100"; Invoke-RestMethod -Uri "$server/processing-jobs/job_xxx" -Method Get -Headers @{ "X-Client-Id" = "resource-crawler" }
```

重试任务：

```powershell
$server = "http://localhost:8100"; Invoke-RestMethod -Uri "$server/processing-jobs/job_xxx/retry" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" }
```

删除资源：

```powershell
$server = "http://localhost:8100"; $body = @{ client_resource_id = "asset-1"; reason = "client delete" } | ConvertTo-Json; Invoke-RestMethod -Uri "$server/processed-resources/delete" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" } -ContentType "application/json" -Body $body
```

客户端统一使用删除接口，不需要判断资源当前是否已经提交到 SearchServer。服务端会先取消同一 `client_id + client_resource_id` 下仍在排队或加工中的任务；如果资源可能已经入库，会向 SearchServer 发起幂等删除；对象清理仅限加工服务器生成的预览，不删除客户端上传的源文件或客户端提供的预览。

## 4. 批量提交

批量接口只是批量创建任务；内部仍然是一个资源一个 job。

```powershell
$server = "http://localhost:8100"; $body = @{ request_id = "batch-1"; manifests = @(@{ client_resource_id = "asset-1"; resource_type = "single_image"; source_object = @{ storage_profile_id = "default"; object_key = "resource-crawler/files/asset-1/asset-1.png"; file_name = "asset-1.png"; file_format = "png" }; source_files = @(@{ file_name = "asset-1.png"; file_format = "png"; is_primary = $true }) }, @{ client_resource_id = "asset-2"; resource_type = "single_image"; source_object = @{ storage_profile_id = "default"; object_key = "resource-crawler/files/asset-2/asset-2.png"; file_name = "asset-2.png"; file_format = "png" }; source_files = @(@{ file_name = "asset-2.png"; file_format = "png"; is_primary = $true }) }) } | ConvertTo-Json -Depth 20; Invoke-RestMethod -Uri "$server/processing-jobs/batch" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" } -ContentType "application/json" -Body $body
```

客户端也可以从本地 DB 中读取已保存 manifest 并批量提交：

```powershell
.\.venv\Scripts\python.exe .\client\Scripts\submit_processing_manifest.py --db-path "G:\ResourceUpload\data\databases\pipeline.db" --processing-server "http://localhost:8100" --client-id "resource-crawler" --batch-size 50
```

## 5. 快照与回放

资源加工服务器会保存每个 `client_id + client_resource_id` 的最新加工快照，用于 SearchServer upsert 失败、清库或重新部署后的恢复。快照只保存可回放的 metadata，不保存二进制，不保存 embedding。

回放单个资源：

```powershell
$server = "http://localhost:8100"; Invoke-RestMethod -Uri "$server/processed-resource-snapshots/asset-1/replay" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" }
```

批量回放全部快照：

```powershell
$server = "http://localhost:8100"; Invoke-RestMethod -Uri "$server/processed-resource-snapshots/replay" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" }
```

只回放上次 upsert 失败的快照：

```powershell
$server = "http://localhost:8100"; Invoke-RestMethod -Uri "$server/processed-resource-snapshots/replay?search_upsert_state=upsert_failed" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" }
```

限制回放数量：

```powershell
$server = "http://localhost:8100"; Invoke-RestMethod -Uri "$server/processed-resource-snapshots/replay?limit=100" -Method Post -Headers @{ "X-Client-Id" = "resource-crawler" }
```

## 6. 描述批处理

客户端可以一个个提交资源，服务端会按数量和等待时间聚合描述生成任务。

常用参数：

- `RP_DESCRIPTION_BATCH_ENABLED`：是否启用服务端描述聚合。
- `RP_DESCRIPTION_BATCH_MIN_SIZE`：达到多少条 pending 描述任务后立即 flush。
- `RP_DESCRIPTION_BATCH_MAX_SIZE`：单批最多多少条。
- `RP_DESCRIPTION_BATCH_MAX_WAIT_SECONDS`：最早 pending 任务最多等待多少秒。

当前执行层保留了 `generate_descriptions_batch` 适配点。provider 原生 Batch API 接入后，可以在该适配点替换掉并发单请求 fallback。

## 7. 常见排障

对象下载失败：

- 检查 `resource_processing_server/storage_profiles.jsonc` 中的 profile、bucket、endpoint、region 是否正确。
- 检查 `OBJECT_STORAGE_ACCESS_KEY` / `OBJECT_STORAGE_SECRET_KEY` 是否已设置。
- 检查 profile 的 `allowed_prefixes` 是否允许对象 key 前缀。

SearchServer upsert 失败：

- 检查 `RP_SEARCH_SERVER_URL` 是否可访问。
- 检查 `RP_SEARCH_SERVER_API_KEY` 或 `RP_SEARCH_SERVER_BEARER_TOKEN`。
- 查看任务状态；失败后可用快照回放接口重试。

描述没有生成：

- 如果 manifest 里有 `provided_description`，服务端会跳过描述生成。
- 检查 `resource_processing_server/.env` 中的 `RP_LLM_PROVIDER` 和 `RP_LLM_MODEL`。
- 检查 `resource_processing_server/.env.local` 中对应 provider 的 API Key。

预览没有生成：

- 如果 manifest 里有 `provided_previews`，服务端会优先校验并复用。
- 检查 Linux/容器环境中的 Blender、ffmpeg、Spine 相关工具是否可用。
- 对需要 GUI 的插件，确认是否需要 Xvfb 或专用 Worker。

## 8. 测试

运行资源加工服务器相关测试：

```powershell
.\.venv\Scripts\python.exe -m pytest resource_processing_server\Test\test_processing_service.py
```

运行关键联动测试：

```powershell
.\.venv\Scripts\python.exe -m pytest client\Test\ResourceProcessor\core\test_upload_to_processing_server.py resource_processing_server\Test\test_processing_service.py resource_processing_server\Test\test_source_files.py SearchServer\Test\CloudService\test_ingest_upsert.py SearchServer\Test\CloudService\test_milvus_search_client.py
```
