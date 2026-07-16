# ResourceUpload 服务端操作指南

除特别说明外，搜索服务器命令都在 `SearchServer` 目录执行。Windows 使用 PowerShell，Linux 使用 Bash。下面的命令都尽量写成单行，复制整行即可运行。

Windows 进入服务端目录：

```powershell
cd G:\ResourceUpload\SearchServer
```

Linux 进入服务端目录：

```bash
cd /path/to/ResourceUpload/SearchServer
```

Windows 下不要直接使用裸 `python`：它可能命中 WindowsApps 的 Python shim。下面的 PowerShell 示例都显式使用仓库里的 `..\.venv\Scripts\python.exe`。

## 1. 准备

安装依赖：

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

公开配置保留在 `SearchServer/.env`（可提交），私有密钥放在 `SearchServer/.env.local`（已被 Git 忽略）。确认两份文件至少包含：

```env
# .env
DATABASE_URL=postgresql+asyncpg://resource:resource@localhost:5432/resource_upload
MILVUS_HOST=localhost
MILVUS_PORT=19530
SERVER_EMBEDDING_PROVIDER=ksyun
SERVER_EMBEDDING_MODEL=qwen3-embedding-8b
SERVER_EMBEDDING_DIMENSION=4096

# .env.local
JWT_SECRET=替换为强随机字符串
API_KEYS=按需配置调用方key
KSPMAS_API_KEY=你的金山云APIKey
```

SearchServer 不上传、读取或代理对象存储内容，只根据 `storage_profile_id + object_key` 生成 CDN URL。bucket、region、CDN 域名和 CDN 鉴权模式放在 `SearchServer/storage_profiles.jsonc`。

如果搜索结果需要返回 CDN 预览/下载 URL，SearchServer 读取同一份 profile；密钥仍然放在 `.env.local`：

```env
# .env.local
OBJECT_STORAGE_CDN_AUTH_KEY_PRIMARY=你的 CDN 鉴权主密钥
OBJECT_STORAGE_CDN_AUTH_KEY_SECONDARY=你的 CDN 鉴权备用密钥
```

如果切换服务端 embedding provider，把公开模型配置留在 `SearchServer/.env`，对应密钥留在 `SearchServer/.env.local`：

```env
# .env
SERVER_EMBEDDING_PROVIDER=dashscope
SERVER_EMBEDDING_MODEL=text-embedding-v4
SERVER_EMBEDDING_DIMENSION=1024

# .env.local
DASHSCOPE_API_KEY=你的DashScopeKey
```

## 2. 服务端

SearchServer 只部署搜索 API、单实例后台 worker、Postgres、Milvus、reranker 等搜索侧依赖。API 的 Gunicorn 进程不再重复启动向量同步和 FTS worker；`background-worker` 独立消费持久队列，避免多进程将数据库连接和后台并发成倍放大。docker compose 里的 MinIO 仅供 Milvus standalone 内部使用，不作为资源文件桶。资源加工服务器单独部署，通过 HTTP 调用 SearchServer 的 `/resources/upsert`、`/resources/delete` 等接口，不要求和 SearchServer 在同一台物理机。搜索结果中的包下载入口是 `package_download_url`；父子资源字段已从搜索响应和资源详情响应中移除。

当前 Compose 保留双向量 worker的既有吞吐，将 Milvus standalone 的 compaction并发限制为一个任务，默认对符合条件的有序 segment使用流式 merge-sort，并提高 etcd session对长时间 GC暂停的容忍度。生产环境仍建议至少16 GiB内存；未经过持续写入和 compaction压测，不要提高 compaction并发。

reranker 会读取挂载的 Hugging Face 缓存中 `hub/models--BAAI--bge-reranker-v2-m3/refs/main`，自动解析当前 `snapshots/<hash>` 目录，无需在 `.env` 写死 `RERANKER_MODEL_PATH`。缓存不存在或不完整时才回退到 Hugging Face 模型名和 ModelScope 路径。

### Windows

启动或重启，保留现有数据：

```powershell
.\start_server.ps1
```

启动并跟随 API 日志：

```powershell
.\start_server.ps1 -Logs
```

如果直接执行 `docker compose up -d --build` 时在 `apt-get update` / `apt-get install` 阶段出现 `502 Bad Gateway`、`Connection failed` 或 `error reading from server: EOF`，先改成逐个构建镜像再启动：

```powershell
docker compose build postgres
docker compose build reranker
docker compose build api
docker compose up -d --no-build
```

如果当前网络访问 Debian / PostgreSQL 官方源不稳定，可以临时指定更稳定的镜像源：

```powershell
$env:DEBIAN_MIRROR="https://你的-debian-镜像/debian"
$env:DEBIAN_SECURITY_MIRROR="https://你的-debian-security-镜像/debian-security"
$env:POSTGRES_APT_MIRROR="https://你的-postgresql-镜像/pub/repos/apt"
docker compose up -d --build
```

清空 Postgres、Milvus（含 Milvus 内部 MinIO 数据卷）后重启：

```powershell
.\start_server.ps1 -Clean
```

参数说明：

- `-Logs`：服务启动完成后继续跟随 `api` 容器日志，适合观察请求和错误。
- `-Clean`：执行 `docker compose down -v`，会删除服务端数据卷，再重新启动服务。仅在确认要清空服务端数据时使用。

### Linux

首次部署或小内存主机升级前，以 root身份执行幂等的主机内存初始化脚本。Swap 文件名默认使用相对值 `swapfile`，脚本会将它解析到默认目录 `/data/ResourceLibrary/swapfile`，即实际创建并持久启用 8 GiB 的 `/data/ResourceLibrary/swapfile/swapfile`，同时设置 `vm.swappiness=10`；已有且已启用的 Swap不会重复创建：

```bash
bash tools/setup_linux_host.sh
```

可通过环境变量覆盖默认值，例如 `DATA_DIR=/mnt/data SWAP_FILE=searchserver.swap SWAP_SIZE_GIB=16 SWAPPINESS=10 bash tools/setup_linux_host.sh`。`SWAP_FILE` 也兼容绝对路径。

启动或重启，保留现有数据：

```bash
docker compose up -d --build
```

如果构建阶段在 apt 下载处出现 `502 Bad Gateway`、`Connection failed` 或 `EOF`，先改成逐个构建镜像再启动：

```bash
docker compose build postgres
docker compose build reranker
docker compose build api
docker compose up -d --no-build
```

跟随 API 日志：

```bash
docker compose logs -f api
```

跟随向量同步和 FTS 后台任务日志：

```bash
docker compose logs -f background-worker
```

清空 Postgres、Milvus（含 Milvus 内部 MinIO 数据卷）后重启：

```bash
docker compose down -v && docker compose up -d --build
```

参数说明：

- `up -d --build`：构建镜像并后台启动容器。
- `logs -f api`：持续输出 `api` 服务日志。
- `logs -f background-worker`：持续输出唯一的向量同步和 FTS worker 日志。
- `down -v`：停止容器并删除数据卷，会清空服务端持久化数据。

### 通用检查

健康检查：

```powershell
..\.venv\Scripts\python.exe .\check_server.py --health
```

查看统计：

```powershell
..\.venv\Scripts\python.exe .\check_server.py --stats
```

查看资源列表：

```powershell
..\.venv\Scripts\python.exe .\check_server.py --resources --page 1 --page-size 20
```

参数说明：

- `--health`：只检查 API、Postgres、Milvus、reranker 是否健康；对象存储不再作为 SearchServer 健康项。
- `--stats`：查看服务端资源总数、状态分布、向量数量等汇总信息。
- `--resources`：分页查看资源列表。
- `--page`：资源列表页码，从 `1` 开始。
- `--page-size`：每页返回多少条资源。

## 3. API 验证

查看服务端统计：

```powershell
..\.venv\Scripts\python.exe .\check_server.py --stats
```

查看资源列表：

```powershell
..\.venv\Scripts\python.exe .\check_server.py --resources --page 1 --page-size 20
```

搜索验证：

```powershell
..\.venv\Scripts\python.exe .\check_server.py --search "角色模型" --search-threshold 0.5 --search-top-k 5
```

参数说明：

- `--stats`：查看服务端资源总数、状态分布、向量数量等汇总信息。
- `--resources`：分页查看服务端资源列表。
- `--search`：通过服务端搜索接口验证语义检索链路。
- `--search-threshold`：最低相似度阈值，越高结果越严格。
- `--search-top-k`：最多返回多少条结果。

本地资源处理流水线见 `client/OPERATIONS_GUIDE.md`。

## 4. 向量库维护

重算所有已提交资源向量：

```powershell
.\.venv\Scripts\python.exe .\rebuild_embeddings.py
```

先演练，不落库：

```powershell
.\.venv\Scripts\python.exe .\rebuild_embeddings.py --dry-run
```

只处理前 100 条：

```powershell
.\.venv\Scripts\python.exe .\rebuild_embeddings.py --limit 100
```

指定 Milvus 写入批大小：

```powershell
.\.venv\Scripts\python.exe .\rebuild_embeddings.py --batch-size 100
```

参数说明：

- `--dry-run`：只演练，不写数据库，也不写 Milvus。
- `--limit`：只处理前 N 条资源。
- `--batch-size`：Milvus 批量写入大小。
- `--recreate-collection`：重建 Milvus collection，默认开启。
- `--no-recreate-collection`：不重建 collection，只刷新现有集合。

## 5. 日志和停服

停服务但保留数据：

```bash
docker compose down
```

查看 API 日志：

```bash
docker compose logs -f api
```

## 6. 最短跑通清单

Windows 启动服务：

```powershell
cd G:\ResourceUpload\SearchServer; .\start_server.ps1
```

Linux 启动服务：

```bash
cd /path/to/ResourceUpload/SearchServer && docker compose up -d --build
```

检查结果：

```powershell
cd G:\ResourceUpload\SearchServer; ..\.venv\Scripts\python.exe .\check_server.py --health
```

搜索验证：

```powershell
cd G:\ResourceUpload\SearchServer; ..\.venv\Scripts\python.exe .\check_server.py --search "角色模型" --search-threshold 0.5 --search-top-k 5
```


