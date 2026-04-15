# 服务端部署配置指南

## 1. 必须修改的配置项

### 1.1 JWT Secret

默认值 `dev-secret-change-in-production` 仅用于开发环境。生产部署必须设置独立的强 secret：

```env
JWT_SECRET=<随机生成的64字符字符串>
```

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
| `DB_POOL_MIN` | `10` | 连接池最小连接数 |
| `DB_POOL_MAX` | `50` | 连接池最大连接数 |

### 2.2 Milvus（向量数据库）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MILVUS_HOST` | `localhost` | Milvus 服务地址 |
| `MILVUS_PORT` | `19530` | Milvus 服务端口 |
| `MILVUS_COLLECTION` | `resource_embeddings` | 集合名称 |

### 2.3 S3 / MinIO（对象存储）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KS3_ENDPOINT` | `http://localhost:9000` | S3 兼容服务地址 |
| `KS3_PUBLIC_ENDPOINT` | `None` | 浏览器可访问的 S3 地址（内网部署时需设置） |
| `KS3_ACCESS_KEY` | `minioadmin` | 访问密钥 |
| `KS3_SECRET_KEY` | `minioadmin` | 秘密密钥 |
| `KS3_BUCKET` | `resources` | 存储桶名称 |
| `KS3_REGION` | `cn-beijing-6` | 区域 |
| `KS3_PRESIGN_EXPIRES` | `3600` | 预签名 URL 有效期（秒） |

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
- [ ] 确认 S3/MinIO 服务可达且 bucket 已创建
- [ ] 设置至少一个 embedding provider 的 API Key
- [ ] 如用 KS3 且客户端通过公网访问，设置 `KS3_PUBLIC_ENDPOINT`

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
