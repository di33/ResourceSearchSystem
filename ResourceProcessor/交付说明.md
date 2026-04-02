# 资源处理系统交付说明

## 一、系统简介
本系统实现了游戏资源的自动筛选、拷贝分类、预览生成、索引管理、动态扩展、完整性校验、异步任务、性能监控等功能。所有功能均通过单元测试，具备高可用性和可扩展性。

## 二、使用方法

### 1. 环境准备
- Python 3.8 及以上
- 依赖库：Pillow、ffmpeg（已配置到 PATH）、shutil、asyncio 等
- 安装依赖：
  ```bash
  pip install pillow
  # 其他依赖如有 requirements.txt 请一并安装
  ```

### 2. 主要入口
- 资源筛选与处理主流程可通过 `resource_filter.py`、`task_manager.py` 等脚本调用。
- 支持命令行或集成到更大系统中。

### 3. 典型用法
1. 配置资源类型（如 resource_types.json）
2. 调用资源筛选、拷贝、预览等接口（可参考各模块的 test_*.py 单元测试用例）
3. 处理完成后，查看输出目录结构和索引文件

## 三、处理后资源结构
假设工作目录为 `output/`，处理后结构如下：

```
output/
├── images/           # 图片资源
│   ├── xxx.png
│   └── ...
├── models/           # 3D模型资源
│   ├── xxx.fbx
│   └── ...
├── animations/       # 动画资源
│   ├── xxx.anim
│   └── ...
├── previews/         # 预览文件（缩略图、GIF、WebP、MP4等）
│   ├── xxx.png
│   ├── xxx.gif
│   ├── xxx.webp
│   └── xxx.mp4
├── resources.json    # 资源索引文件，记录原始与处理后资源、预览、依赖、状态等
└── integrity_check.log # 文件完整性校验日志
```

## 四、资源索引文件说明（resources.json）
- 记录每个资源的：
  - 原始路径、拷贝后路径
  - 预览文件路径
  - 依赖关系（如模型与贴图）
  - 状态（处理中、已完成、失败）
  - 其他扩展字段
- 示例片段：
  ```json
  [
    {
      "original_path": "source/xxx.png",
      "copied_path": "output/images/xxx.png",
      "preview_path": "output/previews/xxx.webp",
      "dependencies": [],
      "status": "completed"
    }
  ]
  ```

## 五、对接下游模块建议
- 下游模块可直接读取 `resources.json` 获取所有已处理资源及其预览、依赖等信息。
- 资源文件已按类型分类，便于批量处理。
- 预览文件统一存放于 `previews/`，支持多格式。
- 完整性校验日志可用于异常追踪。

## 六、扩展与维护
- 新增资源类型：修改 resource_types.json 并实现对应处理类，注册到工厂即可。
- 性能与容量参数可在配置或接口中调整。
- 所有核心功能均有单元测试，便于持续集成。

---
如有疑问请查阅各模块源码及测试用例，或联系开发维护者。