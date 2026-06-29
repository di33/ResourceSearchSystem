# ResourceUpload 客户端操作指南

除特别说明外，命令都在仓库根目录执行。Windows 使用 PowerShell，Linux 使用 Bash。下面的命令都尽量写成单行，复制整行即可运行。

Windows 进入仓库并设置 `PYTHONPATH`：

```powershell
cd G:\ResourceUpload; $env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"
```

Linux 进入仓库并设置 `PYTHONPATH`：

```bash
cd /path/to/ResourceUpload && export PYTHONPATH=/path/to/ResourceUpload/client/Scripts
```

Windows 下不要直接使用裸 `python`：它可能命中 WindowsApps 的 Python shim。下面的 PowerShell 示例都显式使用仓库里的 `.\.venv\Scripts\python.exe`。运行 `ResourceProcessor` 模块命令时，示例会把 `$env:PYTHONPATH` 放在同一行里。

## 1. 准备

安装依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r client\requirements.txt
```

公开配置保留在 `client/.env`（可提交），私有密钥放在 `client/.env.local`（已被 Git 忽略）。确认两份文件至少包含：

```env
# .env
CLIENT_LLM_PROVIDER=ksyun
CLIENT_LLM_BASE_URL=https://kspmas.ksyun.com/v1
KSPMAS_LLM_MODEL=qwen3-vl-235b-a22b-instruct

# .env.local
KSPMAS_API_KEY=你的金山云APIKey
```

如果图片描述走 Codex，音频继续走 API provider：

```env
# .env
CLIENT_LLM_PROVIDER=codex
AUDIO_LLM_PROVIDER=ksyun
AUDIO_LLM_MODEL=mimo-v2.5
CODEX_MODEL=gpt-5.5
CODEX_CONCURRENCY=1

# .env.local
KSPMAS_API_KEY=你的金山云APIKey
```

## 2. 服务端连接

客户端只需要知道服务端 API 地址。服务端启动、停止、Docker 日志和向量重建命令见 `server/OPERATIONS_GUIDE.md`。

默认地址写在 `client/.env`：

```env
TEST_SERVER_URL=http://localhost:8000
```

确认服务端健康：

```powershell
$server = "http://localhost:8000"; Invoke-RestMethod -Uri "$server/health" -Method Get
```

查看服务端统计：

```powershell
$server = "http://localhost:8000"; Invoke-RestMethod -Uri "$server/stats" -Method Get
```

查看资源列表：

```powershell
$server = "http://localhost:8000"; Invoke-RestMethod -Uri "$server/resources?page=1&page_size=20" -Method Get
```

参数说明：

- `$server`：服务端 API 地址，应与 `TEST_SERVER_URL` 一致。
- `/health`：检查 API、Postgres、Milvus、S3、reranker 健康状态。
- `/stats`：查看资源总数、状态分布、向量数量等汇总信息。
- `/resources`：分页查看已上传资源列表。

## 3. 推荐资源流水线

推荐使用 SQLite 拆分流水线：

```text
crawler_state.db -> pipeline.db -> 预览 -> 描述 -> 上传
```

关键边界：

- 只有“同步本地库”读取 `G:\ResourceCrawler\data\crawler_state.db`。
- 预览、描述、上传只读取当前 `pipeline.db`。
- 原始资源文件路径已经记录在 `resource_file`，后续步骤仍会读取这些原始文件。

### 3.1 同步本地库

增量同步：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.tools.sync_pipeline_from_crawler_state --crawler-state-db "G:\ResourceCrawler\data\crawler_state.db" --crawler-output "K:\ResourceCrawler\output" --db-path "G:\ResourceUpload\data\databases\pipeline.db"
```

默认是增量同步：未变更资源保留已有预览和描述；变更资源会清理对应预览/描述并回到待处理状态；已删除资源会清理本地 DB 记录和预览文件。

全量同步：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.tools.sync_pipeline_from_crawler_state --crawler-state-db "G:\ResourceCrawler\data\crawler_state.db" --crawler-output "K:\ResourceCrawler\output" --db-path "G:\ResourceUpload\data\databases\pipeline.db" --clear-first
```

只演练，不写库、不删文件：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.tools.sync_pipeline_from_crawler_state --crawler-state-db "G:\ResourceCrawler\data\crawler_state.db" --crawler-output "K:\ResourceCrawler\output" --db-path "G:\ResourceUpload\data\databases\pipeline.db" --dry-run
```

指定预览目录并跳过备份：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.tools.sync_pipeline_from_crawler_state --crawler-state-db "G:\ResourceCrawler\data\crawler_state.db" --crawler-output "K:\ResourceCrawler\output" --db-path "G:\ResourceUpload\data\databases\pipeline.db" --preview-dir "G:\ResourceUpload\data\workdirs\test_workdir_rebuilt_20260608_150207\previews" --no-backup
```

参数说明：

- `--crawler-state-db`：ResourceCrawler 生成的 `crawler_state.db` 路径，是同步的源数据库。
- `--crawler-output`：ResourceCrawler 的 `output` 根目录，用来定位原始资源文件。
- `--db-path`：目标 `pipeline.db` 路径，预览、描述、上传都会读取这个库。
- `--clear-first`：同步前清空当前 pipeline 表，相当于全量重建。
- `--dry-run`：只统计差异，不写数据库，也不删除预览文件。
- `--preview-dir`：允许清理旧预览文件的目录，可重复传入。未传时会推断常见 `previews` 目录。
- `--no-backup`：执行同步前不备份目标数据库。
- `--keep-preview-files`：只删除 `resource_preview` 记录，不删除磁盘上的预览文件。
- `--replace-db-file`：配合 `--clear-first` 使用，直接删除旧 SQLite 文件后重建。
- `--commit-every`：每处理 N 条新增或变更资源提交一次事务，默认 `1000`。
- `--asset-batch-size`：同步 `asset_index` 的批处理大小，默认 `10000`。

### 3.2 生成预览

断点续跑生成预览：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_previews --db-path "G:\ResourceUpload\data\databases\pipeline.db" --work-dir "G:\ResourceUpload\data\workdirs\test_workdir_rebuilt" --resume
```

强制重建指定资源类型的预览：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_previews --db-path "G:\ResourceUpload\data\databases\pipeline.db" --work-dir "G:\ResourceUpload\data\workdirs\test_workdir_rebuilt" --resource-type single_image --force
```

参数说明：

- `--db-path`：读取和写入的 pipeline SQLite 数据库。
- `--work-dir`：预览输出工作目录，实际图片会写到 `<work-dir>\previews\<resource_type>\`。
- `--resume`：跳过已经达到预览完成状态的资源，用于断点续跑。
- `--force`：强制重新生成匹配资源的预览，成功后清除旧预览记录和旧预览文件。
- `--limit`：最多处理多少个资源。
- `--resource-type`：只处理指定资源类型，例如 `single_image`、`atlas`、`spine_skeleton`。
- `--source-filter`：只处理指定来源站点。
- `--task-id`：只处理指定 task id，可重复传入。
- `--min-task-id`：只处理 id 大于等于该值的 task。
- `--max-task-id`：只处理 id 小于等于该值的 task。

### 3.3 生成描述

使用 `client/.env` / `client/.env.local` 默认 provider：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_descriptions --db-path "G:\ResourceUpload\data\databases\pipeline.db" --resume
```

显式指定 provider：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_descriptions --db-path "G:\ResourceUpload\data\databases\pipeline.db" --llm-provider ksyun --audio-llm-provider ksyun --resume
```

重试失败描述：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_descriptions --db-path "G:\ResourceUpload\data\databases\pipeline.db" --retry-failed --max-retries 3
```

Ksyun 视觉模型响应较慢时，可临时放宽读超时并降低并发后重试：

```powershell
$env:KSPMAS_LLM_TIMEOUT = "180"; $env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_descriptions --db-path "G:\ResourceUpload\data\databases\pipeline.db" --llm-provider ksyun --retry-failed --max-retries 3 --concurrency 3
```

参数说明：

- `--db-path`：读取预览结果并写入描述结果的 pipeline SQLite 数据库。
- `--resume`：跳过已经生成描述的资源，只处理待描述资源。
- `--llm-provider`：图片等非音频资源使用的 LLM provider。未传时读取 `client/.env` / `client/.env.local` 里的 `CLIENT_LLM_PROVIDER`，没有配置则使用 `mock`。
- `--audio-llm-provider`：音频资源使用的 LLM provider。未配置时音频可能会被跳过。
- `--retry-failed`：只重试描述生成失败的任务。
- `--max-retries`：失败任务最多允许重试多少次，默认 `3`。
- `--concurrency`：并发请求数。Codex 默认 `1`，API provider 默认 `5`，也可显式指定。
- `--limit`：最多处理多少个资源。
- `--resource-type`：只处理指定资源类型。
- `--source-filter`：只处理指定来源站点。
- `KSPMAS_LLM_TIMEOUT`：Ksyun LLM 请求读超时秒数，未设置时默认 `60`；大模型批量视觉描述建议 `180`。

### 3.4 上传

先 dry-run 看可上传数量：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.upload_resources --db-path "G:\ResourceUpload\data\databases\pipeline.db" --dry-run
```

正式上传：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.upload_resources --db-path "G:\ResourceUpload\data\databases\pipeline.db" --server "http://localhost:8000"
```

重新上传已提交资源：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.upload_resources --db-path "G:\ResourceUpload\data\databases\pipeline.db" --force
```

参数说明：

- `--db-path`：读取待上传资源的 pipeline SQLite 数据库。
- `--server`：服务端 API 地址，默认读取 `client/.env` / `client/.env.local` 里的 `TEST_SERVER_URL`，否则使用 `http://localhost:8000`。
- `--dry-run`：只统计可上传数量，不实际上传文件和提交资源。
- `--force`：把已提交资源重置为待上传状态，用于服务端清库或换地址后重新上传。
- `--retry-failed`：仅重置上传失败的资源并重新上传。
- `--concurrency`：并发上传数量，默认 `5`。
- `--limit`：最多处理多少个资源。
- `--resource-type`：只上传指定资源类型。
- `--source-filter`：只上传指定来源站点。

## 4. 状态检查

查看本地流水线状态：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -c "from ResourceProcessor.cache.local_cache import LocalCacheStore; c=LocalCacheStore(r'G:\ResourceUpload\data\databases\pipeline.db'); print(c.count_tasks_by_state()); c.close()"
```

查看服务端上传统计：

```powershell
$server = "http://localhost:8000"; Invoke-RestMethod -Uri "$server/stats" -Method Get
```

查看服务端资源列表：

```powershell
$server = "http://localhost:8000"; Invoke-RestMethod -Uri "$server/resources?page=1&page_size=20" -Method Get
```

参数说明：

- `/stats`：查看服务端总览统计。
- `/resources`：查看资源列表。
- `page`：列表页码。
- `page_size`：每页资源数。

## 5. 搜索和下载

搜索：

```powershell
$body = @{ query_text = "角色模型"; top_k = 5; similarity_threshold = 0.5 } | ConvertTo-Json
```

```powershell
$resp = Invoke-RestMethod -Uri "http://localhost:8000/search" -Method Post -ContentType "application/json" -Body $body
```

```powershell
$resp.results[0].file_download_url; $resp.results[0].parent_download_url
```

API 参数说明：

- `query_text`：搜索文本。
- `top_k`：最多返回多少条结果。
- `similarity_threshold`：最低相似度阈值。
- `-Uri`：请求地址。
- `-Method Post`：使用 POST 请求。
- `-ContentType "application/json"`：声明请求体是 JSON。
- `-Body $body`：发送上一步构造出的 JSON 请求体。

下载：

```powershell
New-Item -ItemType Directory -Force -Path ".\downloads" | Out-Null; Invoke-WebRequest -Uri $resp.results[0].file_download_url -OutFile ".\downloads\resource.bin"
```

参数说明：

- `-ItemType Directory`：创建目录。
- `-Force`：目录已存在时不报错。
- `-Path`：目录路径。
- `-Uri`：下载地址。
- `-OutFile`：保存到本地的文件路径。

## 6. 常用维护

描述失败后重试：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_descriptions --db-path "G:\ResourceUpload\data\databases\pipeline.db" --retry-failed --max-retries 3
```

上传前 dry-run：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.upload_resources --db-path "G:\ResourceUpload\data\databases\pipeline.db" --dry-run
```

只重试上传失败的资源：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.upload_resources --db-path "G:\ResourceUpload\data\databases\pipeline.db" --retry-failed
```

重新生成指定资源类型预览：

```powershell
$env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_previews --db-path "G:\ResourceUpload\data\databases\pipeline.db" --resource-type single_image --force
```

参数说明：

- `--retry-failed`：只处理失败状态的任务。
- `--max-retries`：描述生成失败任务最多重试次数。
- `--dry-run`：只统计待上传数量，不实际上传。
- `--force`：强制重新生成匹配资源的预览。
- `--resource-type`：限定处理某一类资源。

## 7. 最短跑通清单

确认服务端可访问：

```powershell
cd G:\ResourceUpload; $server = "http://localhost:8000"; Invoke-RestMethod -Uri "$server/health" -Method Get
```

同步本地库：

```powershell
cd G:\ResourceUpload; $env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.tools.sync_pipeline_from_crawler_state --crawler-state-db "G:\ResourceCrawler\data\crawler_state.db" --crawler-output "K:\ResourceCrawler\output" --db-path "G:\ResourceUpload\data\databases\pipeline.db"
```

生成预览：

```powershell
cd G:\ResourceUpload; $env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_previews --db-path "G:\ResourceUpload\data\databases\pipeline.db" --resume
```

生成描述和分类：

```powershell
cd G:\ResourceUpload; $env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.generate_descriptions --db-path "G:\ResourceUpload\data\databases\pipeline.db" --resume
```

上传：

```powershell
cd G:\ResourceUpload; $env:PYTHONPATH = "G:\ResourceUpload\client\Scripts"; .\.venv\Scripts\python.exe -m ResourceProcessor.upload_resources --db-path "G:\ResourceUpload\data\databases\pipeline.db"
```

检查结果：

```powershell
cd G:\ResourceUpload; $server = "http://localhost:8000"; Invoke-RestMethod -Uri "$server/stats" -Method Get
```

搜索验证：

```powershell
cd G:\ResourceUpload; $server = "http://localhost:8000"; $body = @{ query_text = "角色模型"; top_k = 5; similarity_threshold = 0.5 } | ConvertTo-Json; Invoke-RestMethod -Uri "$server/search" -Method Post -ContentType "application/json" -Body $body
```


