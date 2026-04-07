# 通用数字资源语义检索系统

本地端 + 云端分离架构。本地完成资源预览、描述、向量的生成与缓存，云端负责注册、上传、提交、检索与下载。

---

## 目录结构

```
ResourceUpload/
│
├── Client/                          # 本地客户端
│   ├── Scripts/
│   │   ├── run_resource_pipeline.py # CLI 入口：筛选 → 预览 → 索引
│   │   └── ResourceProcessor/       # 客户端 Python 包
│   │       ├── preview_metadata.py  # 公共数据模型 (ProcessState, PreviewInfo, RPE)
│   │       ├── preview/             # 预览生成
│   │       │   ├── thumbnail_generator.py
│   │       │   ├── pipeline_incremental.py
│   │       │   └── blender_render_fbx_frames.py
│   │       ├── description/         # LLM 描述生成 & 校验
│   │       │   ├── description_generator.py
│   │       │   └── description_validator.py
│   │       ├── embedding/           # 向量生成
│   │       │   └── embedding_generator.py
│   │       ├── cache/               # 本地缓存 & 去重
│   │       │   ├── local_cache.py
│   │       │   └── dedup_strategy.py
│   │       └── core/                # 资源筛选 & 任务管理
│   │           ├── resource_filter.py
│   │           ├── task_manager.py
│   │           └── deps.py
│   ├── Test/                        # 客户端测试（按模块子目录）
│   │   └── ResourceProcessor/
│   │       ├── preview/
│   │       ├── description/
│   │       ├── embedding/
│   │       ├── cache/
│   │       └── core/
│   └── resource_types.json          # 支持的文件扩展名配置
│
├── Server/                          # 云端服务
│   ├── Scripts/
│   │   └── CloudService/            # 云端 Python 包
│   │       ├── cloud_client.py      # 注册/上传/提交 API 合约 & Mock
│   │       ├── search_client.py     # 检索/下载 API 合约 & Mock & Agent 工具
│   │       ├── download_service.py  # 下载服务层
│   │       ├── upload_orchestrator.py # 上传编排
│   │       └── acceptance.py        # 验收清单 & 交付计划
│   └── Test/
│       └── CloudService/
│
├── Design/                          # 设计文档
├── specs.md                         # 12 个 Spec 实施清单
├── pytest.ini                       # pytest 配置
├── conftest.py                      # 测试路径初始化
├── run_tests.py                     # unittest 运行脚本（备选）
└── requirements.txt                 # Python 依赖
```

---

## 环境要求

| 项目 | 版本 |
|------|------|
| Python | >= 3.10（推荐 3.12） |
| Pillow | >= 10.0 |
| pytest | >= 9.0 |
| pytest-asyncio | >= 0.23 |
| Blender（可选） | >= 3.0，仅 FBX 旋转预览 GIF 需要 |

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 运行全部测试

```bash
# 推荐方式（使用 pytest）
python -m pytest

# 备选方式（使用 unittest）
python run_tests.py
```

预期输出：`196 passed`。

### 3. 运行资源处理流水线（本地端 CLI）

```bash
python Client/Scripts/run_resource_pipeline.py \
    --source /path/to/your/resources \
    --work-dir /path/to/output
```

参数说明：

| 参数 | 必填 | 说明 |
|------|------|------|
| `--source` | 是 | 原始资源根目录，递归扫描 |
| `--work-dir` | 是 | 工作输出目录，生成 images/models/others/previews 子目录 |
| `--config` | 否 | `resource_types.json` 路径，默认 `Client/resource_types.json` |
| `--max-file-size` | 否 | 单文件字节上限，超出跳过 |
| `--max-file-count` | 否 | 最多处理文件数 |
| `--no-previews` | 否 | 跳过预览生成，仅拷贝分类和索引 |

产出：
- `{work-dir}/images/` `models/` `others/` — 按类型分类的资源副本
- `{work-dir}/previews/` — 预览缩略图（WebP 优先，长边 512）
- `{work-dir}/resources.json` — 资源索引清单
- `{work-dir}/.pipeline_state.json` — 增量处理状态（源文件未变则跳过）

### 4. FBX 模型预览（可选）

优先自动发现系统 Blender 路径。也可手动指定：

```bash
set BLENDER_EXE=C:\Program Files\Blender Foundation\Blender 4.0\blender.exe
python Client/Scripts/run_resource_pipeline.py --source ... --work-dir ...
```

未找到 Blender 时自动退化为占位 GIF（512×512 灰底 + 文件名标注）。

---

## 配置文件

### resource_types.json

控制哪些文件扩展名会被纳入处理：

```json
{
  "supported_extensions": [
    ".png", ".jpg", ".jpeg", ".gif", ".tga", ".webp",
    ".fbx", ".obj", ".stl"
  ]
}
```

默认位于 `Client/resource_types.json`，可通过 `--config` 指定自定义路径。

---

## 模块说明

### Client — 本地处理

| 模块 | 功能 |
|------|------|
| `preview/thumbnail_generator` | 图片缩略图（长边 512, WebP 优先）、FBX 旋转 GIF、占位降级 |
| `preview/pipeline_incremental` | 增量流水线：指纹比对 → 跳过/拷贝 → 预览 → 质量校验 |
| `preview_metadata` | `ProcessState` 状态机、`PreviewInfo`、`ResourceProcessingEntity` |
| `description/description_generator` | LLM 描述接口抽象、Mock 实现、`LLMFactory` |
| `description/description_validator` | 格式/字数/关键词校验、重试、主备模型切换 |
| `embedding/embedding_generator` | Embedding 接口抽象、Mock 实现、维度校验、重试 |
| `cache/local_cache` | SQLite 持久化：6 张表、按 content_md5 查询、断点恢复 |
| `cache/dedup_strategy` | 去重判定：全量复用 / 重跑描述 / 重跑向量 / 恢复中断 |
| `core/resource_filter` | 文件筛选、分类拷贝、完整性校验、资源索引 |
| `core/task_manager` | 并发任务管理、性能指标 |

### Server — 云端服务

| 模块 | 功能 |
|------|------|
| `cloud_client` | `register` / `upload_file` / `upload_preview` / `commit` API 合约 + Mock |
| `search_client` | `search` / `get_download_link` API 合约 + Mock + Agent 工具入参出参 |
| `download_service` | 下载链接生成、有效期控制、小文件 Base64、Agent 适配器 |
| `upload_orchestrator` | 注册 → 上传 → 提交全流程编排，本地状态同步 |
| `acceptance` | 17 项验收清单（安全/容错/性能/质量/集成）+ 6 阶段交付计划 |

---

## 处理状态机

```
discovered → preview_ready → description_ready → embedding_ready → package_ready
     ↓              ↓                ↓                  ↓
preview_failed  description_failed  embedding_failed   ...
                                                        ↓
                                              registered → uploaded → committed → synced
```

每个阶段失败不会阻塞其他资源，会记录错误码并在下次运行时自动恢复。

---

## 去重与增量策略

| 场景 | 行为 |
|------|------|
| 内容未变 + 配置未变 | `reuse_all`，直接复用缓存 |
| 内容未变 + Prompt 版本变 | `rerun_description`，重跑描述和向量 |
| 内容未变 + Embedding 模型变 | `rerun_embedding`，仅重跑向量 |
| 处理中断（preview_ready 等中间态） | `resume`，从断点继续 |
| 全新内容 | `new`，完整处理 |

判定依据：`content_md5`（文件内容哈希），不依赖文件名。

---

## LLM / Embedding 提供者

### 已集成：DashScope（阿里云通义千问）

项目已内置 DashScope Provider，同时覆盖多模态描述生成和文本向量化，开箱即用。

**1. 安装依赖**

```bash
pip install dashscope
```

**2. 设置 API Key**

从 [阿里云百炼控制台](https://dashscope.console.aliyun.com/) 获取 API Key，然后：

```bash
# Linux / macOS
export DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx

# Windows PowerShell
$env:DASHSCOPE_API_KEY = "sk-xxxxxxxxxxxxxxxxxxxxxxxx"

# Windows CMD
set DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

**3. 使用 DashScope LLM 生成描述**

```python
# 导入后自动注册到 LLMFactory，provider 名称为 "dashscope" 或 "qwen-vl"
import ResourceProcessor.description.dashscope_llm_provider  # noqa: F401

from ResourceProcessor.description.description_generator import (
    DescriptionInput,
    generate_resource_description,
)

# 使用 qwen-vl-max 多模态模型（支持图片理解）
result = await generate_resource_description(
    DescriptionInput(
        preview_path="/path/to/preview.webp",
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={"format": "png", "resolution": "1024x768"},
    ),
    provider_name="dashscope",  # 默认使用 qwen-vl-max
)

print(result.main_content)    # 主体描述
print(result.detail_content)  # 细节描述
print(result.full_description)  # 完整两段式
```

可选模型：
| 名称 | 说明 | 适用场景 |
|------|------|----------|
| `qwen-vl-max` | 最强视觉理解（默认） | 高质量描述 |
| `qwen-vl-plus` | 速度更快、成本更低 | 大批量处理 |

```python
# 使用更快的 qwen-vl-plus
result = await generate_resource_description(input_data, "dashscope", model="qwen-vl-plus")
```

**4. 使用 DashScope Embedding 生成向量**

```python
# 导入后自动注册到 EmbeddingFactory，provider 名称为 "dashscope" 或 "text-embedding-v3"
import ResourceProcessor.embedding.dashscope_embedding_provider  # noqa: F401

from ResourceProcessor.embedding.embedding_generator import generate_embedding_with_retry

# 使用 text-embedding-v3（默认 1024 维）
result, error = await generate_embedding_with_retry(
    "这是一个高质量的 3D 游戏角色模型，适用于 RPG 类游戏开发。",
    provider_name="dashscope",
)

if result:
    print(f"维度: {result.vector_dimension}")  # 1024
    print(f"校验和: {result.embedding_checksum}")
```

可选维度（text-embedding-v3）：1024（默认）、768、512、256、128、64

```python
# 使用 768 维向量
result, error = await generate_embedding_with_retry(text, "dashscope", dimension=768)
```

### 已集成：智谱 AI（GLM-5.1 / GLM-4V）

项目已内置智谱 Provider。LLM 描述默认使用 **GLM-5.1**（纯文本，推理更强），也支持 **GLM-4V-Plus**（多模态，含图片理解）。向量化使用 **embedding-3**。

**1. 安装依赖**

```bash
pip install zhipuai
```

**2. 设置 API Key**

从 [智谱开放平台](https://open.bigmodel.cn/) 获取 API Key，然后：

```bash
# Linux / macOS
export ZHIPUAI_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxx

# Windows PowerShell
$env:ZHIPUAI_API_KEY = "xxxxxxxxxxxxxxxxxxxxxxxx"
```

**3. 使用 GLM 生成描述**

```python
import ResourceProcessor.description.zhipu_llm_provider  # noqa: F401

from ResourceProcessor.description.description_generator import (
    DescriptionInput,
    generate_resource_description,
)

# 默认使用 GLM-5.1（纯文本，强推理）
result = await generate_resource_description(input_data, provider_name="zhipu")

# 使用 GLM-4V-Plus（多模态，读取预览图片生成描述）
result = await generate_resource_description(input_data, "zhipu", model="glm-4v-plus")
```

可选模型：
| 名称 | 类型 | 说明 |
|------|------|------|
| `glm-5.1` | 文本（默认） | 最新最强，编程 & 推理能力接近 Opus 4.6 |
| `glm-5` | 文本 | 上一版，745B MoE |
| `glm-4v-plus` | 多模态 | 支持图片理解，适合根据预览图生成描述 |
| `glm-4v-flash` | 多模态 | 免费，速度快，精度略低 |

**4. 使用智谱 Embedding 生成向量**

```python
import ResourceProcessor.embedding.zhipu_embedding_provider  # noqa: F401

from ResourceProcessor.embedding.embedding_generator import generate_embedding_with_retry

# embedding-3，默认 2048 维
result, error = await generate_embedding_with_retry("资源描述文本", provider_name="zhipu")
```

可选维度（embedding-3）：2048（默认）、1024、512、256

```python
# 使用 1024 维
result, error = await generate_embedding_with_retry(text, "zhipu", dimension=1024)
```

### 自定义 Provider 扩展

也可接入其他服务（OpenAI 等）：

```python
from ResourceProcessor.description.description_generator import BaseMultiModalLLMProvider, LLMFactory

class MyLLMProvider(BaseMultiModalLLMProvider):
    async def generate_description(self, input_data):
        # 调用其他 LLM API
        ...

LLMFactory.register("my_llm", MyLLMProvider)
```

```python
from ResourceProcessor.embedding.embedding_generator import BaseEmbeddingProvider, EmbeddingFactory

class MyEmbeddingProvider(BaseEmbeddingProvider):
    async def generate_embedding(self, text: str):
        ...
    def expected_dimension(self) -> int:
        return 1536
    def model_version(self) -> str:
        return "text-embedding-3-small"

EmbeddingFactory.register("openai", MyEmbeddingProvider)
```

---

## 云端 API 替换

当前 `MockCloudClient` 和 `MockSearchClient` 模拟云端行为。接入真实后端：

```python
from CloudService.cloud_client import BaseCloudClient

class RealCloudClient(BaseCloudClient):
    async def register(self, request):
        # POST /resources/register
        ...
    async def upload_file(self, resource_id, file_path, file_size):
        ...
    async def upload_preview(self, resource_id, preview_path):
        ...
    async def commit(self, request):
        # POST /resources/commit
        ...
```

---

## 验收清单概览

通过 `CloudService.acceptance.build_default_checklist()` 可获取完整的 17 项验收清单：

| 类别 | 数量 | 示例 |
|------|------|------|
| 安全 (security) | 4 | API Key 不落盘、传输加密、路径遍历防护、审计日志 |
| 容错 (fault_tolerance) | 5 | 预览失败降级、描述重试+主备切换、上传幂等、断点恢复、巡检修复 |
| 性能 (performance) | 2 | 检索 P95 < 500ms、批量 100 资源 < 30min |
| 质量 (quality) | 3 | 预览兼容性、描述格式合格率 > 95%、向量维度一致 |
| 集成 (integration) | 3 | Agent 工具联调、状态机完整覆盖、本地→云端全链路 |

---

## 阶段交付计划

| 阶段 | 名称 | 对应 Spec | 退出标准 |
|------|------|-----------|----------|
| 1 | 预览基线 | Spec 1-2 | 预览生成 + 质量校验 + 元数据字段齐备 |
| 2 | 描述 & 向量 | Spec 3-5 | LLM 描述 + 校验重试 + Embedding 向量 |
| 3 | 缓存 & 去重 | Spec 6-7 | SQLite 持久化 + 增量复用策略 |
| 4 | 云端上传 | Spec 8-9 | 注册 → 上传 → 提交全流程通过 |
| 5 | 检索 & 下载 | Spec 10-11 | 搜索/下载 API + Agent 工具合约 |
| 6 | 验收上线 | Spec 12 | 17 项验收全部标绿 |

---

## 常见问题

**Q: 测试报 `ModuleNotFoundError: No module named 'ResourceProcessor'`**

确保从仓库根目录运行 `python -m pytest`，`pytest.ini` 会自动设置 `pythonpath`。不要从子目录运行。

**Q: Blender 预览生成失败**

设置 `BLENDER_EXE` 环境变量指向 Blender 可执行文件路径。未安装 Blender 时自动使用占位 GIF，不影响后续流程。

**Q: 如何只运行某个模块的测试？**

```bash
python -m pytest Client/Test/ResourceProcessor/preview/ -v     # 预览模块
python -m pytest Server/Test/CloudService/ -v                   # 云端模块
python -m pytest -k "test_dedup" -v                             # 按名称匹配
```

**Q: 如何查看验收清单和交付计划？**

```python
from CloudService.acceptance import build_default_checklist, build_delivery_plan

checklist = build_default_checklist()
print(checklist.summary())

for stage in build_delivery_plan():
    print(f"阶段 {stage.stage}: {stage.name} — {stage.specs}")
```
