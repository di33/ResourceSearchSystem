# ResourceUpload 操作指南（常用命令）

本文档汇总日常最常用命令，覆盖：

- 启动/重启服务器
- 查看/清空服务端数据
- 语义搜索
- 运行 `预览 -> 描述 -> 上传` 全流程（含 ResourceCrawler 断点续跑与 `--upload-only` 仅上传）

默认在仓库根目录 `G:/ResourceUpload` 执行（PowerShell）。

---

## 0. 运行前准备

### 0.1 安装依赖

```powershell
pip install -r requirements.txt
```

### 0.2 配置金山云 Key（用于描述和向量）

编辑 `.env`，至少确认：

```env
CLIENT_LLM_PROVIDER=ksyun
SERVER_EMBEDDING_PROVIDER=ksyun
KSPMAS_API_KEY=你的金山云APIKey
```

---

## 1. 启动 / 重启服务器

### 1.1 推荐：脚本方式（Windows）

#### 启动或重启（保留现有数据）

```powershell
.\start_server.ps1
```

#### 启动后跟随 API 日志

```powershell
.\start_server.ps1 -Logs
```

#### 全量重置后启动（会清空数据库/Milvus/MinIO 卷）

```powershell
.\start_server.ps1 -Clean
```

### 1.2 手工 Docker 命令

#### 启动（后台）

```powershell
docker compose up -d --build
```

#### 重启

```powershell
docker compose restart
```

#### 停止

```powershell
docker compose down
```

#### 查看日志

```powershell
docker compose logs -f api
```

### 1.3 健康检查

```powershell
python .\check_server.py --health
```

或：

```powershell
Invoke-RestMethod http://localhost:8000/health
```

---

## 2. 查看 / 清空服务端数据

## 2.1 查看数据

### 查看总体统计（DB + Milvus + S3）

```powershell
python .\check_server.py --stats
```

### 查看资源列表

```powershell
python .\check_server.py --resources --page 1 --page-size 20
```

### 查看某个资源详情

```powershell
python .\check_server.py --detail res-xxxxxxxxxxxxxxxx
```

### 查看 MinIO 对象

```powershell
python .\check_server.py --storage
```

---

## 2.2 清空数据

### 方式 A（推荐，最干净）：清空所有服务端持久化数据

```powershell
.\start_server.ps1 -Clean
```

等价手工命令：

```powershell
docker compose down -v
docker compose up -d --build
```

说明：会清空 Docker volumes（Postgres、Milvus、MinIO）。

### 方式 B：仅停服务不清数据

```powershell
docker compose down
```

---

## 3. 搜索（语义检索）

### 3.1 用检查脚本发起搜索

```powershell
python .\check_server.py --search "角色模型" --search-threshold 0.5 --search-top-k 5
```

提示：`check_server.py` 会打印每条结果的 `preview_url`、主资源 `download_url`，以及存在父整包时的 `parent_download_url`。

### 3.2 直接调用 API

```powershell
$body = @{
  query_text = "角色模型"
  top_k = 5
  similarity_threshold = 0.5
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://localhost:8000/search" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

### 3.3 下载资源

默认推荐直接使用 `/search` 返回的 `file_download_url`。

- 单文件资源：通常直接下载原始主文件
- 多文件资源：返回预打包 ZIP
- `pack` 资源：返回整包 ZIP
- 若命中的是子资源，结果里还会带 `parent_download_url`，可直接下载父整包

`/download` 仍然保留，主要用于按 `resource_id` 重新签发下载链接或做兜底查询。

#### 方式 A：直接使用 `/search` 返回的下载链接

```powershell
$body = @{
  query_text = "扑克牌 UI 按钮"
  top_k = 5
  similarity_threshold = 0.5
} | ConvertTo-Json

$resp = Invoke-RestMethod `
  -Uri "http://localhost:8000/search" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body

$first = $resp.results[0]
$first.file_download_url
$first.parent_download_url
```

#### 方式 B：调用 `/download` 获取临时下载链接

```powershell
$body = @{
  resource_id = "res-xxxxxxxxxxxxxxxx"
  expire_seconds = 3600
  return_base64 = $false
} | ConvertTo-Json

$resp = Invoke-RestMethod `
  -Uri "http://localhost:8000/download" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body

$resp.download_url
```

#### 第二步：把链接下载到本地文件

```powershell
Invoke-WebRequest `
  -Uri $resp.download_url `
  -OutFile ".\downloads\resource.bin"
```

可按返回的原文件名保存：

```powershell
New-Item -ItemType Directory -Force -Path ".\downloads" | Out-Null
Invoke-WebRequest `
  -Uri $resp.download_url `
  -OutFile (Join-Path ".\downloads" $resp.file_name)
```

---

## 4. 运行“预览 -> 描述 -> 上传资源”全流程

项目内置端到端脚本：`test_pipeline.py`，会执行：

1. 扫描与筛选
2. 预览生成
3. 描述生成（按 `.env` 的 `CLIENT_LLM_PROVIDER`）
4. 上传并提交（服务端按 `SERVER_EMBEDDING_PROVIDER` 自动生成向量）

### 4.1 全流程（推荐）

```powershell
python .\test_pipeline.py `
  --source "D:\your_resources" `
  --work-dir ".\test_workdir_seq"
```

### 4.2 仅本地处理，不上传（调试预览/描述）

```powershell
python .\test_pipeline.py `
  --source "D:\your_resources" `
  --work-dir ".\test_workdir_seq" `
  --no-upload
```

### 4.3 跳过预览（仅调试描述/上传）

```powershell
python .\test_pipeline.py `
  --source "D:\your_resources" `
  --work-dir ".\test_workdir_seq" `
  --no-previews
```

### 4.4 指定服务端地址

```powershell
python .\test_pipeline.py `
  --source "D:\your_resources" `
  --server "http://localhost:8000"
```

### 4.5 运行 ResourceCrawler 资源级全流程

当资源已经由 `ResourceCrawler` 预处理为 `output/metadata/*.jsonl` + `output/assets/...` 时，使用专用入口：

```powershell
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "G:\ResourceCrawler\output" `
  --work-dir ".\test_workdir_crawler"
```

这条流水线会执行：

1. 读取 `resource_index.jsonl` 作为资源级入口
2. 用 `index.jsonl` 和包级 JSON 补全 `pack_name`、`resource_path`、`tags`、`description`
3. 生成资源级缩略图
4. 生成 LLM 描述
5. 上传文件、预览并提交

### 4.6 仅本地处理，不上传

```powershell
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "G:\ResourceCrawler\output" `
  --work-dir ".\test_workdir_crawler" `
  --no-upload
```

### 4.7 只处理某一类资源

```powershell
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "G:\ResourceCrawler\output" `
  --work-dir ".\test_workdir_crawler_tileset" `
  --resource-type "tileset" `
  --limit 20 `
  --no-upload
```

常见类型：
- `single_image`
- `tileset`
- `animation_sequence`
- `audio_file`
- `font_file`
- `structured_resource`

### 4.8 只处理某个来源

```powershell
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "G:\ResourceCrawler\output" `
  --source-filter "kenney" `
  --limit 50 `
  --no-upload
```

### 4.9 输出说明

运行后可重点查看：
- `{work-dir}\crawler_resources.jsonl`：资源级映射结果明细，便于确认 `pack_name/resource_path/tags` 是否正确并支持断点续跑
- `{work-dir}\test_results.jsonl`：逐条描述/上传结果明细
- `{work-dir}\previews\`：资源级缩略图
- `{work-dir}\test_results.json`：描述与上传结果摘要

说明：
- 对有实体文件的资源，会执行完整上传
- 对 `metadata-only` 资源，当前策略是保留本地预览和描述，但默认跳过原始文件上传与提交

### 4.10 断点续跑（预览 + 描述，不上传）

用于中断后继续：会读取 `{work-dir}\crawler_resources.jsonl` 与 `{work-dir}\test_results.jsonl` 中已有进度，只补未完成的预览与描述，**不向服务端上传**。

```powershell
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "K:\ResourceCrawler\output" `
  --work-dir "G:\ResourceUpload\test_workdir_all_previews_desc" `
  --resume `
  --no-upload
```

### 4.11 仅上传（已有预览与描述）

前提：`{work-dir}` 下已有 `crawler_resources.jsonl` 与 `test_results.jsonl`，且对应资源具备**有效本地预览路径**与非空 **`description_full`**。脚本会按 `resource_index.jsonl` 扫描目录，仅对满足条件的记录调用服务端注册与上传。

```powershell
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "K:\ResourceCrawler\output" `
  --work-dir "G:\ResourceUpload\test_workdir_all_previews_desc" `
  --upload-only
```

可选参数（与完整流水线相同）：`--server`、`--limit`、`--resource-type`、`--source-filter`。

说明：

- `--upload-only` 与 `--no-upload` **不能同时使用**。
- 需要持久化控制台输出时，可自行重定向，例如：  
  `... --upload-only 2>&1 | Tee-Object -FilePath ".\test_workdir_all_previews_desc\pipeline_upload_only.log"`  
  脚本默认**不会**自动创建该日志文件。

---

## 5. 拆分流水线（SQLite 状态管理）

新方案将 `预览 -> 描述 -> 上传` 拆为三个独立脚本，用 SQLite 管理资源状态，支持高效断点续传和去重。

```
DISCOVERED --(generate_previews)--> PREVIEW_READY --(generate_descriptions)--> DESCRIPTION_READY --(upload_resources)--> COMMITTED
```

### 5.0 迁移旧数据到 SQLite

将已有的 JSONL 状态（`crawler_resources.jsonl` / `test_results.jsonl`）迁移到 SQLite。

```powershell
cd Client\Scripts

python -m ResourceProcessor.tools.migrate_jsonl_to_sqlite `
    --resources-jsonl "G:\ResourceUpload\test_workdir_all_previews_desc\crawler_resources.jsonl" `
    --results-jsonl "G:\ResourceUpload\test_workdir_all_previews_desc\test_results.jsonl" `
    --db-path "G:\ResourceUpload\pipeline.db" `
    --crawler-output "K:\ResourceCrawler\output"
```

参数说明：
- `--crawler-output`：读取 `resource_index.jsonl` 补全原始文件路径（~3秒，推荐加上）
- `--dry-run`：只报告不写入
- 迁移用 `content_md5` 去重，重复运行不会创建重复记录

### 5.1 生成预览

```powershell
python -m ResourceProcessor.generate_previews `
    --crawler-output "K:\ResourceCrawler\output" `
    --db-path "G:\ResourceUpload\pipeline.db" `
    --limit 100
```

可选参数：`--resume`（跳过已完成）、`--resource-type`、`--source-filter`、`--work-dir`

### 5.2 生成描述

```powershell
python -m ResourceProcessor.generate_descriptions `
    --crawler-output "K:\ResourceCrawler\output" `
    --db-path "G:\ResourceUpload\pipeline.db" `
    --llm-provider ksyun `
    --limit 100
```

可选参数：`--resume`、`--retry-failed`（重试失败任务）、`--max-retries 3`

### 5.3 上传资源

```powershell
python -m ResourceProcessor.upload_resources `
    --crawler-output "K:\ResourceCrawler\output" `
    --db-path "G:\ResourceUpload\pipeline.db" `
    --limit 100
```

可选参数：`--server`、`--resume`、`--dry-run`

### 5.4 断点续传

每个脚本都支持 `--resume`，会跳过已达到目标状态的资源。任意一步中断后重跑即可从断点继续。

```powershell
# 中断后重跑预览，只处理未完成的
python -m ResourceProcessor.generate_previews `
    --crawler-output "K:\ResourceCrawler\output" `
    --db-path "G:\ResourceUpload\pipeline.db" `
    --resume
```

### 5.5 查看状态统计

```powershell
cd Client\Scripts
python -c "
from ResourceProcessor.cache.local_cache import LocalCacheStore
cache = LocalCacheStore('G:\ResourceUpload\pipeline.db')
for state, count in cache.count_tasks_by_state().items():
    print(f'{state}: {count}')
cache.close()
"
```

---

## 6. 常用组合（复制即用）

### 重置环境后跑一轮全流程

```powershell
.\start_server.ps1 -Clean
python .\test_pipeline.py --source "D:\your_resources" --work-dir ".\test_workdir_seq"
python .\check_server.py --stats
python .\check_server.py --search "角色模型" --search-threshold 0.5 --search-top-k 5
```

### 用 ResourceCrawler 输出跑一轮资源级流程

```powershell
.\start_server.ps1
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "G:\ResourceCrawler\output" `
  --source-filter "kenney" `
  --limit 50 `
  --work-dir ".\test_workdir_crawler"
python .\check_server.py --resources --page 1 --page-size 20
```

### 预览与描述已就绪，仅批量上传

```powershell
.\start_server.ps1
python .\Client\Scripts\run_crawler_resource_pipeline.py `
  --crawler-output "K:\ResourceCrawler\output" `
  --work-dir "G:\ResourceUpload\test_workdir_all_previews_desc" `
  --upload-only
python .\check_server.py --stats
```

### 仅验证服务是否可用

```powershell
.\start_server.ps1
python .\check_server.py --health
```

---

## 7. 全量重新生成向量（切换向量模型后）

新增脚本：`rebuild_embeddings.py`。用途：对所有已提交资源重算向量，并刷新 Milvus 向量库。

### 7.1 正式执行（推荐）

```powershell
python .\rebuild_embeddings.py
```

默认行为：

- 读取 `.env` 的 `SERVER_EMBEDDING_PROVIDER / MODEL / DIMENSION`
- 遍历 `process_state=committed` 的资源
- 重新生成向量并更新 `resource_embedding` 元数据
- 重建并写入 Milvus 集合（默认会先 drop 再 create）

### 7.2 先演练（不落库）

```powershell
python .\rebuild_embeddings.py --dry-run
```

### 7.3 只处理前 N 条（灰度）

```powershell
python .\rebuild_embeddings.py --limit 100
```

### 7.4 不重建集合，仅增量重写向量

```powershell
python .\rebuild_embeddings.py --no-recreate-collection
```

