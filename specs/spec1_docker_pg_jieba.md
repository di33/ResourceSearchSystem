# Spec 1: Docker 基础设施 — pg_jieba 扩展

## 目标
PostgreSQL 容器支持 pg_jieba 中文分词扩展。

## 交付物
1. `docker/postgres/Dockerfile` — 基于 postgres:16-bookworm 编译安装 pg_jieba
2. `docker/postgres/init-pg-jieba.sql` — CREATE EXTENSION IF NOT EXISTS pg_jieba
3. `docker-compose.yml` — postgres 服务改用自定义 Dockerfile + 挂载 init 脚本

## 详细规格

### 1.1 `docker/postgres/Dockerfile`
```dockerfile
FROM postgres:16-bookworm

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    ca-certificates \
    postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/*

# Build pg_jieba from source
RUN git clone --depth 1 https://github.com/jaiminpan/pg_jieba.git /tmp/pg_jieba \
    && cd /tmp/pg_jieba \
    && git submodule update --init --recursive \
    && make USE_PGXS=1 \
    && make USE_PGXS=1 install \
    && rm -rf /tmp/pg_jieba

# Clean up build deps (optional, keeps image smaller)
RUN apt-get purge -y build-essential git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
```

### 1.2 `docker/postgres/init-pg-jieba.sql`
```sql
CREATE EXTENSION IF NOT EXISTS pg_jieba;
```

### 1.3 `docker-compose.yml` 变更
- postgres 服务删除 `image: postgres:16-alpine`
- 改为:
```yaml
postgres:
    build:
      context: ./docker/postgres
      dockerfile: Dockerfile
    ports:
      - "5432:5432"
    environment:
      POSTGRES_USER: resource
      POSTGRES_PASSWORD: resource
      POSTGRES_DB: resource_upload
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./docker/postgres/init-pg-jieba.sql:/docker-entrypoint-initdb.d/init-pg-jieba.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U resource -d resource_upload"]
      interval: 5s
      timeout: 3s
      retries: 10
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
```

## 验证方式
```bash
docker compose build postgres
docker compose up postgres -d
docker compose exec postgres psql -U resource -d resource_upload -c "SELECT * FROM pg_extension WHERE extname='pg_jieba';"
# 应返回一行记录
```

## 单元测试
此 spec 为基础设施，无 Python 单元测试。验证通过 docker compose build + SQL 查询完成。
