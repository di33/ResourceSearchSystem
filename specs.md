# 通用数字资源语义检索系统实施 Spec 流程

本文基于 `Design/通用数字资源语义检索系统（本地端+云端分离）技术设计文档（修订版）.md` 拆解实施顺序。

拆解原则：
- 每个 Spec 都是可独立开发、测试、验收的最小闭环。
- Spec 顺序遵循真实依赖关系，优先打通本地预览，再进入描述、向量、缓存、上传和检索。
- 每个 Spec 都尽量对应现有代码起点，避免只有概念没有落点。

---

## Spec 1: 预览载体现状核查与缩略图能力修正
### 描述
先核查当前预览生成实现是否满足修订版设计文档要求，再修正图片缩略图、模型预览降级、命名规则和质量校验逻辑。这是后续描述生成和向量生成的前置条件。

### 输入
- 设计文档第 3 章“资源预览载体设计”与第 4.1 节状态机要求。
- 当前实现文件：`Scripts/ResourceProcessor/thumbnail_generator.py`、`Scripts/ResourceProcessor/pipeline_incremental.py`。
- 当前测试文件：`Test/ResourceProcessor/test_thumbnail_generator.py`、`Test/ResourceProcessor/test_pipeline_incremental.py`。

### 输出
- 一份与设计文档对齐的预览生成基线。
- 明确记录并修正当前实现差距。
- 能通过测试验证的预览生成行为。

### 最小可执行单元
- 对照设计文档核查当前差距，并明确以下现状是否需要修改：图片默认仍为 `256x256`、图片预览当前产物为 `png`、模型预览当前仅稳定覆盖 `.fbx`、缺少质量校验与 `preview_strategy` 字段。
- 将图片预览调整为符合设计要求的长边 `512`、短边等比缩放。
- 按设计文档补齐预览命名规则，优先输出 `{content_md5}_preview.{ext}`。
- 为模型预览定义可执行的降级顺序：优先 GIF，不满足时退化为关键帧拼图或静态首帧。
- 在预览生成后增加质量校验，至少覆盖文件存在、分辨率、体积、非全黑、非全透明、非空白。
- 失败时将资源标记为 `preview_failed`，阻止进入后续描述生成。
- 更新和补齐对应测试，确保首个 Spec 可以独立验收。

---

## Spec 2: 预览元数据与本地状态字段补齐
### 描述
为预览载体建立统一数据契约，把预览格式、尺寸、策略和处理状态写入本地处理实体，作为描述生成和缓存复用的稳定输入。

### 输入
- 设计文档第 4.4.2 节本地处理实体定义。
- Spec 1 产出的预览生成基线。

### 输出
- 统一的 `preview` 结构。
- 可驱动状态机推进的本地字段定义。

### 最小可执行单元
- 在本地处理实体中补齐 `preview.strategy`、`preview.path`、`preview.format`、`preview.width`、`preview.height`、`preview.size`。
- 增加 `process_state` 的最小状态闭环：`discovered -> preview_ready`，失败态为 `preview_failed`。
- 约定 `preview_strategy` 至少支持 `static`、`gif`、`contact_sheet`。
- 约定 `preview_renderer`、`used_placeholder` 等调试字段的保留方式，便于兼容性排查。
- 确保下游描述模块不再只依赖单一 `preview_file_path`。

---

## Spec 3: LLM 标准描述生成基础实现
### 描述
基于标准化预览载体和辅助元数据生成可用于检索的标准描述文本，形成从 `preview_ready` 到 `description_ready` 的第一个闭环。

### 输入
- 已完成的预览载体及其元数据。
- `resource_type`、必要辅助元数据、`preview_strategy`。
- LLM Provider 配置。

### 输出
- `main_content`
- `detail_content`
- `full_description`
- `prompt_version`

### 最小可执行单元
- 定义统一的描述生成接口 `generate_description()`。
- 按设计文档要求实现两段式输出：主体描述和细节描述。
- 将 `preview` 内容、资源类型和辅助元数据组合为稳定输入。
- 为 Prompt 引入显式版本号，如 `prompt_v1`。
- 产出可直接传给 Embedding 模块的 `full_description`。

---

## Spec 4: 描述校验、重试与主备模型切换
### 描述
在描述生成能力可用后，补齐格式校验、字数校验、关键词校验、重试和主备模型切换逻辑，保证描述质量稳定可控。

### 输入
- Spec 3 输出的描述文本。
- 主模型与备用模型配置。

### 输出
- 校验通过的标准描述。
- `description_failed` 失败态与错误信息。

### 最小可执行单元
- 校验输出是否严格匹配两行结构。
- 校验字数是否落入设计文档要求的容忍区间。
- 校验是否包含资源类型或等价词。
- 失败时最多重试 `2` 次。
- 主模型连续失败时自动切换备用模型。
- 超过阈值后写入错误码、错误信息，并将状态置为 `description_failed`。

---

## Spec 5: Embedding 向量生成与结果校验
### 描述
将标准描述文本转换为固定维度向量，并对维度、类型和空值进行校验，形成 `description_ready -> embedding_ready` 的闭环。

### 输入
- `full_description`
- `embedding_provider`
- `embedding_model_version`

### 输出
- `vector_data`
- `vector_dimension`
- `embedding_checksum`
- `embedding_generate_time`

### 最小可执行单元
- 定义统一的向量生成接口。
- 调用 Embedding 服务生成一维浮点数组。
- 校验向量维度与模型配置一致。
- 失败时自动重试 `2` 次。
- 超过阈值后写入 `embedding_failed` 状态。

---

## Spec 6: 本地处理实体与缓存存储
### 描述
建立本地处理实体和持久化缓存，为断点恢复、结果复用、错误追踪和后续上传编排提供稳定基础。

### 输入
- 预览、描述、向量三个阶段的结构化结果。
- 本地缓存路径与数据库配置。

### 输出
- 本地处理实体。
- 可查询、可恢复的本地缓存存储。

### 最小可执行单元
- 定义包含 `content_md5`、`resource_id`、`source_file`、`preview`、`description`、`embedding`、`process_state` 的本地处理实体。
- 使用 SQLite 建立最小表集：`resource_task`、`resource_preview`、`resource_description`、`resource_embedding`、`resource_upload_job`、`process_log`。
- 保存最近错误码、错误信息、更新时间和重试次数。
- 提供按 `content_md5` 和本地任务 ID 查询的能力。
- 为断点恢复预留状态恢复入口。

---

## Spec 7: 去重、复用与增量重跑策略
### 描述
在已有缓存基础上，建立去重、复用和增量重跑规则，避免重复生成预览、描述和向量，同时允许在策略变更时精确重算。

### 输入
- `content_md5`
- 本地缓存记录
- 预览策略、Prompt 版本、Embedding 模型版本

### 输出
- 可复用或需重算的明确判定结果。
- 面向批处理的增量执行策略。

### 最小可执行单元
- 优先使用 `content_md5` 作为资源去重主键，而不是文件名。
- 对内容未变化且配置版本未变化的资源直接复用缓存结果。
- 当预览策略变更时，只重跑预览及受影响下游阶段。
- 当 Prompt 或 Embedding 模型版本变更时，只重跑描述和向量阶段。
- 为占位预览、失败态资源和中断任务定义再次执行规则。

---

## Spec 8: 云端注册与上传协议
### 描述
打通本地端到云端的第一段协议，完成注册、上传模式选择和原始文件/预览文件上传能力。

### 输入
- 本地处理完成的资源基础信息。
- `content_md5`、资源类型、文件大小、预览格式。

### 输出
- 注册结果。
- 上传目标与上传模式。
- 原始文件和预览载体的上传结果。

### 最小可执行单元
- 实现 `POST /resources/register` 的请求与响应契约。
- 生成并传递 `idempotency_key`。
- 支持根据文件大小选择 `direct` 或 `multipart` 上传模式。
- 同时上传原始资源文件和预览载体文件。
- 为上传失败写入 `upload_failed` 及失败阶段信息。

---

## Spec 9: 提交接口与云端状态推进
### 描述
在文件上传成功后，通过提交接口完成元数据落库、向量写入和索引更新，形成资源入库闭环。

### 输入
- 已上传的原始文件与预览载体。
- 描述信息和向量信息。
- `resource_id` 与 `idempotency_key`。

### 输出
- 云端最终状态或异步任务 ID。
- 可检索资源的入库结果。

### 最小可执行单元
- 实现 `POST /resources/commit` 请求体与响应体。
- 校验原始文件和预览载体是否完整上传。
- 写入元数据、描述和向量。
- 推进云端状态 `registered -> object_uploaded -> metadata_saved -> vector_saved -> indexed -> available`。
- 任一步失败时记录错误码、失败阶段并进入补偿流程。

---

## Spec 10: 检索接口与 Agent 预览工具契约
### 描述
面向消费侧提供稳定的检索预览能力，让 Agent 和业务方可以先搜索、先看预览，再决定是否下载资源。

### 输入
- 查询文本。
- 资源类型、格式过滤、TopK、相似度阈值。

### 输出
- 检索结果列表。
- 相似度分数。
- 预览 URL、摘要信息和改写建议。

### 最小可执行单元
- 实现 `POST /resources/search` 的基础能力。
- 返回 `resource_id`、`resource_type`、`score`、`preview_url`、`description_summary`、`file_format`、`file_size`、`status`、`preview_available`。
- 仅返回 `available` 状态资源。
- 输出与 Agent 工具 `search_digital_resource_preview` 一致的数据结构。
- 当结果为空时返回推荐改写词、可放宽过滤条件和阈值调整建议。

---

## Spec 11: 下载接口与 Agent 下载工具契约
### 描述
在检索结果可用后，提供独立的下载地址生成能力，满足“先预览、后下载”的接入原则。

### 输入
- `resource_id`
- 下载有效期
- 是否返回 Base64

### 输出
- 下载地址
- 过期时间
- 文件信息
- 可选 Base64 内容

### 最小可执行单元
- 实现 `POST /resources/download-link` 的请求和返回结构。
- 为小文件提供可选 Base64 返回能力。
- 为普通资源返回预签名下载地址。
- 对不可下载资源返回明确错误码，如 `RESOURCE_NOT_FOUND`、`RESOURCE_NOT_AVAILABLE`、`PERMISSION_DENIED`。
- 输出与 Agent 工具 `get_digital_resource_download_link` 保持一致。

---

## Spec 12: 安全、容错、验收与阶段交付
### 描述
将跨模块的安全要求、容错补偿、性能指标、验收清单和阶段交付物收敛为最终上线前的总体验收 Spec。

### 输入
- 前 11 个 Spec 的实现结果。
- 设计文档第 7、8、9、10、13 章约束。

### 输出
- 可执行的上线验收清单。
- 阶段性交付与风险兜底方案。

### 最小可执行单元
- 为密钥管理、访问控制和审计日志定义落地要求。
- 为本地端和云端失败场景定义重试、补偿和巡检规则。
- 将关键性能指标转为可验证的测试口径，如检索响应、批量处理耗时和描述格式合格率。
- 将“预览兼容性验证、描述质量验证、向量维度校验、本地状态机恢复验证、上传幂等验证、Agent 接口联调验证”写入验收项。
- 将整体工作与实施阶段对应起来，便于分阶段交付和回归验收。