# 资源处理与管理设计文档

## 目标
设计一个系统，能够从指定目录中筛选资源，生成缩略图/GIF，并管理资源与预览文件的对应关系，支持未来扩展。

---

## 系统功能概述

### 1. 资源筛选
- 从指定目录中筛选支持的资源类型。
- 支持动态扩展资源类型。
- **支持的资源类型描述字段**：
  - `priority`: 处理优先级。
  - `enabled`: 是否启用该资源类型。
- **资源类型校验**：
  - 在筛选资源时，增加对文件完整性（如文件头信息）的校验，避免处理损坏的文件。

### 2. 资源拷贝与分类
- 将筛选出的资源拷贝到工作目录。
- 按资源类型分类存储。
- **分类规则扩展**：
  - 支持基于元数据（如文件大小、创建时间）的分类。
  - 示例：大文件（>100MB）存储到 `large_files/` 目录。
- **文件冲突处理**：
  - 增加对文件名冲突的处理逻辑（如重命名或覆盖策略）。
  - 在索引文件中记录原始文件名。

### 3. 缩略图/GIF 生成
- 根据资源类型生成对应的预览文件：
  - 图片：缩略图。
  - 模型：渲染缩略图。
  - 动画：生成 GIF。
- **生成策略优化**：
  - 对于大文件或复杂模型，支持异步生成缩略图，并提供生成进度的查询接口。
  - 增加对生成失败的重试机制，避免因偶发错误导致任务中断。
- **多格式支持**：
  - 支持更多预览格式（如 WebP、MP4），以适应不同场景需求。

### 4. 资源与预览管理
- 生成资源索引文件，记录资源与预览文件的对应关系。
- 索引文件格式：JSON。
- **索引文件优化**：
  - 增加对资源依赖关系的记录（如模型与贴图的关联）。
  - 示例：
```json
{
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "dependencies": ["texture1.png", "texture2.png"]
}
```
- **状态管理**：
  - 增加对资源状态的详细描述（如处理中、已完成、失败）。
  - 提供状态变更的时间戳。

---

## 目录结构设计
```
processed_resources/
├── images/
├── models/
├── animations/
├── previews/
│   ├── images/
│   ├── models/
│   ├── animations/
├── large_files/
└── resources.json
```
- **用途说明**：
  - `large_files/`：存储大文件（>100MB）。
  - 其他目录与文件用途保持不变。

---

## 扩展性设计

### 动态资源类型支持
- 使用配置文件定义支持的资源类型及其处理方式。
- **配置文件示例**：
```json
{
  "image": {"extensions": ["png", "jpg"], "priority": 1, "enabled": true},
  "model": {"extensions": ["obj", "fbx"], "priority": 2, "enabled": true},
  "animation": {"extensions": ["mp4", "avi"], "priority": 3, "enabled": true},
  "vfx": {"extensions": ["vfx_extension"], "priority": 4, "enabled": false}
}
```

### 统一处理接口
- 定义资源处理接口，每种资源类型实现自己的处理方法。
- **接口定义示例**：
```python
class ResourceProcessor:
    def process(self, resource_path: str, output_dir: str) -> str:
        """处理资源并返回生成的预览路径"""
        pass
```

### 新增资源类型
- 实现新的处理类并注册到处理器工厂中。
- **注册步骤**：
  1. 修改配置文件，添加新资源类型。
  2. 实现对应的处理逻辑。
  3. 在处理器工厂中注册新类型。

---

## 任务队列与 Worker
- 使用消息队列（如 Redis + RQ 或 Celery）管理任务：
  - 主进程扫描资源并将任务加入队列。
  - Worker 从队列中拉取任务，执行处理逻辑。
  - 任务完成后更新索引状态。
- **优先级规则**：
  - 资源密集型任务（如模型渲染）优先处理。
  - 轻量任务（如图片缩略图生成）可并行处理。
- **Worker 配置示例**：
```python
from rq import Queue
from redis import Redis

redis_conn = Redis()
queue = Queue(connection=redis_conn)
queue.enqueue(process_task, resource_path)
```

---

## 容器化工具
- 将 Blender 和 FFmpeg 等工具封装进 Docker 容器：
  - 确保一致的运行环境。
  - 隔离资源密集型任务，避免影响主机性能。
- **Dockerfile 示例**：
```dockerfile
FROM ubuntu:20.04
RUN apt-get update && apt-get install -y blender ffmpeg
CMD ["/bin/bash"]
```

---

## 可观察性与日志
- 为每个任务生成唯一 trace_id，记录处理日志。
- **日志格式示例**：
```json
{
  "trace_id": "abc123",
  "task": "generate_preview",
  "status": "success",
  "duration": 5.2,
  "error": null
}
```
- 上报处理时长、成功/失败率、错误分类等指标。
- 设置失败任务的报警机制。

---

## 并发与资源控制
- 使用 Worker Pool 控制并发数。
- 对模型渲染等资源密集型任务设置单独队列，限制并发数。
- **配置示例**：
```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=4) as executor:
    executor.submit(process_task, resource_path)
```

---

## 技术栈
- **语言**：Python
- **工具**：
  - 图片处理：Pillow
  - 模型渲染：Blender 或 PyMeshLab
  - 动画处理：FFmpeg
  - 文件操作：shutil
  - 消息队列：Redis + RQ 或 Celery
  - 容器化：Docker

---

## 下一步计划
1. 编写资源筛选与拷贝功能。
2. 实现缩略图/GIF 生成逻辑。
3. 生成资源索引文件。
4. 配置任务队列与 Worker。
5. 容器化工具环境。
6. 增加文件完整性校验与分类规则扩展。
7. 实现生成失败的重试机制与多格式支持。