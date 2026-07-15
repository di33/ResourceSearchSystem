# 服务器一键启动

`manage_servers.py` 会统一管理全部服务器：

- `SearchServer`
- `preview_renderer`
- `resource_processing_server`

不指定服务名时，命令默认同时操作以上三台服务器。

## 一键启动全部服务器

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe .\manage_servers.py start
```

Linux / macOS：

```bash
python3 manage_servers.py start
```

## 构建并启动全部服务器

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe .\manage_servers.py start --build
```

Linux / macOS：

```bash
python3 manage_servers.py start --build
```

构建脚本会把 reranker 的大体积 Python 依赖，以及 preview-renderer / resource-processor
共用的系统、Python 和 Node.js 依赖保存为带内容指纹的本地基础镜像。首次构建仍需安装依赖；
依赖文件没有变化时，后续构建会直接复用基础镜像，只重新复制业务代码。请优先通过
`manage_servers.py build` 构建，不要直接清理 `resource-upload/*-base:*` 镜像。

依赖来源可通过 `RP_VENDOR_MODE` 控制：默认 `auto` 在 vendor 不完整时自动在线安装；
`required` 适用于已准备完整 apt/pip/npm vendor 的离线环境；`online` 强制忽略 vendor。

每个镜像构建默认最多等待 30 分钟，超时会终止完整的 Docker/Buildx
子进程树并明确报错，不会无限挂起。可按需调整，例如：

```bash
python3 manage_servers.py start --build --build-timeout 3600
```

启动 SearchServer 或 resource-processing-server 时，管理脚本会先单独启动
对应 PostgreSQL，并把已有数据卷中的角色密码同步为各服务
`.env.local` 中的 `POSTGRES_PASSWORD`。因此修改密码后不需要删除已有数据卷。

## 常用管理命令

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe .\manage_servers.py status
.\.venv\Scripts\python.exe .\manage_servers.py health
.\.venv\Scripts\python.exe .\manage_servers.py restart
.\.venv\Scripts\python.exe .\manage_servers.py stop
```

Linux / macOS：

```bash
python3 manage_servers.py status
python3 manage_servers.py health
python3 manage_servers.py restart
python3 manage_servers.py stop
```

## 只操作指定服务器

可在命令末尾追加服务名：`search`、`renderer`、`processor`。

示例：

```powershell
.\.venv\Scripts\python.exe .\manage_servers.py restart renderer processor
```

```bash
python3 manage_servers.py restart renderer processor
```

## 其他参数

- `--no-wait`：启动后不等待健康检查。
- `--timeout 600`：将健康检查等待时间设置为 600 秒。
- `--wait-reranker`：启动 SearchServer 时等待 reranker 模型完全加载成功；不加时允许 SearchServer 先以 `degraded` 状态启动。
- `--volumes`：停止或重启时同时删除 Docker Compose 数据卷。
- `clean`：停止服务器并清空相关运行数据。
- `reset --build`：清空数据、重新构建并启动全部服务器。

查看 reranker 是否已完全就绪：

```bash
python3 manage_servers.py health search
```
