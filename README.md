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
- `--volumes`：停止或重启时同时删除 Docker Compose 数据卷。
- `clean`：停止服务器并清空相关运行数据。
- `reset --build`：清空数据、重新构建并启动全部服务器。
