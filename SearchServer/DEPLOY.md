# 服务端部署配置指南

## 1. 必须修改的配置项

### 1.1 JWT Secret

默认值 `dev-secret-change-in-production` 仅用于开发环境。生产部署必须设置独立的强 secret：

```env
JWT_SECRET=<随机生成的64字符字符串>
```

本地开发时把真实值写入 `.env.local`，`.env` 只保留可提交的默认值或空值。

生成方式（PowerShell）：

```powershell
-join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
```

### 1.2 CORS Origins

生产环境不允许 `allow_origins=["*"]`。`debug=False` 时 CORS 默认关闭（`allow_origins=[]`）。

如需前端直连，需要在 `app/main.py` 中配置允许的域名列表，或通过环境变量控制：

```python
# app/main.py 中按需修改
allowed_origins = [
    "https://your-frontend-domain.com",
]
```

---

## 2. 完整环境变量列表

### 2.1 PostgreSQL

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql+asyncpg://resource:resource@localhost:5432/resource_upload` | 数据库连接串 |
| `DB_POOL_MIN` | `8`（Compose API） | 每个进程的连接池常驻连接数；Gunicorn 多进程会按进程数累加 |
| `DB_POOL_MAX` | `16`（Compose API） | 每个进程的连接池最大连接数；4 个 API 进程合计最多 64 |

Compose 使用独立的单进程 `background-worker` 消费向量同步和 FTS 持久队列，连接池为 `4..8`。API 容器设置 `VECTOR_SYNC_WORKER_ENABLED=false` 和 `FTS_WORKER_ENABLED=false`，避免每个 Gunicorn 进程重复启动后台任务。

### 2.2 Milvus（向量数据库）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MILVUS_HOST` | `localhost` | Milvus 服务地址 |
| `MILVUS_PORT` | `19530` | Milvus 服务端口 |
| `MILVUS_COLLECTION` | `resource_embeddings` | 集合名称 |

### 2.3 资源 URL / CDN

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OBJECT_STORAGE_CDN_AUTH_KEY_PRIMARY` | `""` | CDN Type A 主鉴权密钥，放在 `.env.local` 或系统环境变量 |
| `OBJECT_STORAGE_CDN_AUTH_KEY_SECONDARY` | `""` | CDN Type A 备鉴权密钥，放在 `.env.local` 或系统环境变量 |

SearchServer 不上传、读取或代理对象存储内容，只根据 `storage_profile_id + object_key` 生成 CDN 访问 URL。bucket、region、CDN、URL 鉴权模式等 profile 配置放在 `SearchServer/storage_profiles.jsonc`；CDN 鉴权密钥继续通过 `.env.local` 或系统环境变量注入。SearchServer 侧不需要配置对象存储 endpoint 或 AK/SK。

`default` 可以作为别名指向真实 profile id，例如 `game-ai-studio-resource-1252100362`；新写入的 manifest/DB 应保存真实 profile id。

`SearchServer/storage_profiles.jsonc` 中的 CDN 鉴权字段对应腾讯云控制台：

| Profile 字段 | 腾讯云配置 |
|--------------|------------|
| `cdn_auth_type` | 鉴权模式，例如 `A` |
| `cdn_auth_algorithm` | 鉴权算法，例如 `md5` |
| `cdn_auth_sign_param` | 签名参数，例如 `sign` |
| `cdn_auth_expires` | 有效时间，单位秒，例如 `600` |
| `cdn_auth_time_format` | 时间格式，例如 `unix_decimal` |
| `cdn_auth_scope` | 鉴权范围，例如 `file_suffix` |
| `cdn_auth_file_suffixes` | 文件后缀，`["*"]` 表示所有文件 |

CDN 示例：

```env
OBJECT_STORAGE_CDN_AUTH_KEY_PRIMARY=你的 CDN 主鉴权密钥
OBJECT_STORAGE_CDN_AUTH_KEY_SECONDARY=你的 CDN 备鉴权密钥
```

`OBJECT_STORAGE_CDN_AUTH_KEY_PRIMARY` / `OBJECT_STORAGE_CDN_AUTH_KEY_SECONDARY` 放在 `SearchServer/.env.local` 或部署环境变量中，不要提交到 Git。

### 2.4 向量生成

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SERVER_EMBEDDING_PROVIDER` | `ksyun` | 向量服务提供商：`ksyun` / `dashscope` / `zhipu` |
| `SERVER_EMBEDDING_MODEL` | `embedding-3` | 向量模型名称 |
| `SERVER_EMBEDDING_DIMENSION` | `1024` | 向量维度 |
| `SERVER_EMBEDDING_BASE_URL` | `https://kspmas.ksyun.com/v1` | API 基础地址（ksyun） |
| `KSPMAS_API_KEY` | `""` | 金山云 API Key |
| `KSC_API_KEY` | `""` | 备用 API Key |
| `DASHSCOPE_API_KEY` | `""` | 阿里云 DashScope Key（provider=dashscope 时必填） |
| `ZHIPUAI_API_KEY` | `""` | 智谱 AI Key（provider=zhipu 时必填） |

### 2.5 安全

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `JWT_SECRET` | `dev-secret-change-in-production` | **必须修改** |
| `JWT_ALGORITHM` | `HS256` | 签名算法 |
| `JWT_EXPIRE_MINUTES` | `60` | Token 有效期 |
| `DEBUG` | `False` | 设为 `True` 时跳过 JWT 认证、开放 CORS |

---

## 3. 部署检查清单

- [ ] 设置强随机 `JWT_SECRET`
- [ ] 设置 `DEBUG=False`
- [ ] 配置允许的 CORS origins（如需前端直连）
- [ ] 确认 PostgreSQL 连接串和凭据
- [ ] 确认 Milvus 服务可达
- [ ] 如搜索结果需要对象 URL，确认 CDN 配置可用
- [ ] 设置至少一个 embedding provider 的 API Key
- [ ] 不要在 SearchServer 配置对象存储 AK/SK

---

## 4. 向量库重建

切换向量模型或 provider 后，需要重建向量：

```powershell
# 先演练（不落库）
python .\rebuild_embeddings.py --dry-run

# 正式执行
python .\rebuild_embeddings.py
```

重建会 drop 并重建 Milvus collection，用 `resource_id` 作为主键。
