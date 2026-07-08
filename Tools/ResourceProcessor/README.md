# ResourceProcessor Tools

`Tools/ResourceProcessor` 只保留可复用的预览、描述和数据适配能力。它不提供面向用户的操作命令，也不访问客户端、服务器或数据库。

调用边界：

- 客户端/服务端负责读取和写入自己的数据库、配置与对象存储。
- 客户端/服务端把已经准备好的输入数据传给 Tools。
- Tools 返回预览结果、描述结果或中间结构，由调用者决定如何持久化。

主要模块：

| 模块 | 功能 |
|------|------|
| `preview/` | 图片、模型和资源级预览生成工具 |
| `preview/runtime/` | 只由预览运行时调用的 helper 脚本 |
| `crawler/records.py` | crawler 资源/资产记录的轻量数据结构 |
| `crawler/resource_adapter.py` | 将调用者提供的 crawler 记录映射成通用资源实体 |
| `description/description_generator.py` | LLM 抽象、描述生成和校验 |
| `core/task_manager.py` | 可复用的异步任务队列 |
