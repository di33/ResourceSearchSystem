# ResourceUpload 服务端操作指南

除特别说明外，服务端命令都在 `server` 目录执行。Windows 使用 PowerShell，Linux 使用 Bash。下面的命令都尽量写成单行，复制整行即可运行。

Windows 进入服务端目录：

```powershell
cd G:\ResourceUpload\server
```

Linux 进入服务端目录：

```bash
cd /path/to/ResourceUpload/server
```

Windows 下不要直接使用裸 `python`：它可能命中 WindowsApps 的 Python shim。下面的 PowerShell 示例都显式使用仓库里的 `..\.venv\Scripts\python.exe`。

## 1. 准备

安装依赖：

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

公开配置保留在 `server/.env`（可提交），私有密钥放在 `server/.env.local`（已被 Git 忽略）。确认两份文件至少包含：

```env
# .env
DATABASE_URL=postgresql+asyncpg://resource:resource@localhost:5432/resource_upload
MILVUS_HOST=localhost
MILVUS_PORT=19530
KS3_ENDPOINT=http://localhost:9000
SERVER_EMBEDDING_PROVIDER=ksyun
SERVER_EMBEDDING_MODEL=qwen3-embedding-8b
SERVER_EMBEDDING_DIMENSION=4096

# .env.local
JWT_SECRET=替换为强随机字符串
API_KEYS=按需配置调用方key
KSPMAS_API_KEY=你的金山云APIKey
```

腾讯 COS 可以这样配置；公开项可放 `.env`，真实密钥只放 `.env.local`：

```env
# .env
KS3_ENDPOINT=https://cos.ap-guangzhou.myqcloud.com
KS3_BUCKET=game-ai-studio-resource-1252100362
KS3_REGION=ap-guangzhou
KS3_SIGNATURE_VERSION=s3v4
KS3_ADDRESSING_STYLE=virtual
KS3_CDN_ENDPOINT=https://gameai-studio.seasungame.com

# .env.local
KS3_ACCESS_KEY=你的 SecretId
KS3_SECRET_KEY=你的 SecretKey
```

如果切换服务端 embedding provider，把公开模型配置留在 `server/.env`，对应密钥留在 `server/.env.local`：

```env
# .env
SERVER_EMBEDDING_PROVIDER=dashscope
SERVER_EMBEDDING_MODEL=text-embedding-v4
SERVER_EMBEDDING_DIMENSION=1024

# .env.local
DASHSCOPE_API_KEY=你的DashScopeKey
```

## 2. 服务端

### Windows

启动或重启，保留现有数据：

```powershell
.\start_server.ps1
```

启动并跟随 API 日志：

```powershell
.\start_server.ps1 -Logs
```

清空 Postgres、Milvus、MinIO 后重启：

```powershell
.\start_server.ps1 -Clean
```

参数说明：

- `-Logs`：服务启动完成后继续跟随 `api` 容器日志，适合观察请求和错误。
- `-Clean`：执行 `docker compose down -v`，会删除服务端数据卷，再重新启动服务。仅在确认要清空服务端数据时使用。

### Linux

启动或重启，保留现有数据：

```bash
docker compose up -d --build
```

跟随 API 日志：

```bash
docker compose logs -f api
```

清空 Postgres、Milvus、MinIO 后重启：

```bash
docker compose down -v && docker compose up -d --build
```

参数说明：

- `up -d --build`：构建镜像并后台启动容器。
- `logs -f api`：持续输出 `api` 服务日志。
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

- `--health`：只检查 API、Postgres、Milvus、MinIO 是否健康。
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
cd G:\ResourceUpload\server; .\start_server.ps1
```

Linux 启动服务：

```bash
cd /path/to/ResourceUpload/server && docker compose up -d --build
```

检查结果：

```powershell
cd G:\ResourceUpload\server; ..\.venv\Scripts\python.exe .\check_server.py --health
```

搜索验证：

```powershell
cd G:\ResourceUpload\server; ..\.venv\Scripts\python.exe .\check_server.py --search "角色模型" --search-threshold 0.5 --search-top-k 5
```


