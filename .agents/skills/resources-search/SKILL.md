---
name: resources-search
description: "搜索2D游戏素材资源。当需要查找图片、图集、瓦片、精灵、动画、音效等游戏素材时使用。也适用于搜索UI元素、角色立绘、场景背景、道具图标等游戏制作所需的视觉和音频资源。"
---

# 2D 游戏素材搜索

当制作游戏需要素材资源时，通过本工具的语义搜索 API 查找合适的资源。搜索支持自然语言描述，基于向量语义匹配返回最相关的结果。

> **注意：搜索服务仅限内网访问。**

## 搜索步骤

### 1. 搜索资源

向搜索 API 发送自然语言描述，获取匹配的素材列表：

```
POST http://10.11.101.112:8000/search
Content-Type: application/json
Authorization: Bearer {TOKEN}
```

TOKEN 为 JWT（HS256），由服务端生成，默认有效期 60 分钟。调试模式下可跳过认证。

请求体：

```json
{
  "query_text": "像素风地牢地面贴图",
  "resource_type": "single_image",
  "format_filter": ["png"],
  "top_k": 5,
  "similarity_threshold": 0.6
}
```

**参数说明：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `query_text` | 是 | 自然语言描述要找什么素材 |
| `resource_type` | 否 | 资源类型过滤，以下列出常用类型 |
| `format_filter` | 否 | 文件格式过滤，如 `["png", "wav"]` |
| `top_k` | 否 | 返回条数，默认 10 |
| `similarity_threshold` | 否 | 最低相似度阈值，默认 0.5，建议以 0.1 为步长调整 |

**常用资源类型：**

| 类型 | 适用场景 |
|------|----------|
| `single_image` | 单张图片：角色立绘、UI图标、道具图、场景元素 |
| `tileset` | 瓦片集：地面、墙壁、地形等可拼接贴图 |
| `animation_sequence` | 动画序列帧 |
| `atlas` | 图集/雪碧图 |
| `audio_file` | 音效、背景音乐 |
| `pack` | 资源包（含多个文件的合集） |

`pack` 类型结果中包含 `contains_resource_types` 字段，标明资源包内含有的资源类型，例如：

```json
"contains_resource_types": ["single_image", "tileset", "atlas"]
```

### 2. 评估结果

每条搜索结果包含以下关键字段：

- **`score`**：相似度分数（0-1），> 0.7 为高质量匹配
- **`primary_preview_url`**：预览图 URL，用于查看素材外观
- **`description_summary`**：素材描述摘要
- **`file_format`** 和 **`file_size`**：格式和大小
- **`file_download_url`**：S3 预签名下载链接，默认有效期 1 小时，最长 24 小时，无需携带 TOKEN 即可直接访问
- **`parent_*` 字段**：如果素材属于某个资源包，会包含父资源信息

### 3. 无结果时的处理

如果搜索返回空结果，检查响应中的 `suggestion` 字段：

- `rewrite_queries`：尝试用建议的改写词重新搜索
- `relaxable_filters`：考虑移除某些过滤条件
- `suggested_threshold`：降低相似度阈值
- `try_cross_type`：是否需要跨类型搜索

直接使用搜索结果中的 `file_download_url` 即可下载素材，该链接为 S3 预签名 URL，无需携带认证信息，过期后需重新搜索获取新链接。

## 游戏素材搜索示例

| 需求场景 | query_text 示例 |
|----------|----------------|
| 主角角色图 | 卡通风格勇者角色 立绘 |
| 地图瓦片 | 像素风草地地面 tileset |
| UI 按钮 | 圆角按钮 游戏UI 绿色 |
| 敌人图集 | 哥布林怪物 sprite sheet |
| 背景音乐 | 轻松欢快 村庄BGM |
| 攻击音效 | 剑挥砍 剑气音效 |
| 道具图标 | 红药水 回复道具 icon |
| 特效素材 | 火焰燃烧 粒子特效 |
| 场景背景 | 像素风森林 背景 parallax |
| 字体文件 | 像素风 像素字体 游戏用 |

## 搜索技巧

1. **描述越具体越好**：像素风红色巨龙 boss 立绘 比 龙 效果好
2. **加上风格关键词**：像素风、卡通、写实、Q版
3. **加上用途关键词**：UI、tileset、立绘、icon、背景
4. **首次搜索用默认阈值**：先用 0.5 阈值看结果范围，再根据需要以 0.1 为步长调整
5. **资源包优先**：搜到 `pack` 类型的结果往往包含成套素材，查看 `contains_resource_types` 了解内容
