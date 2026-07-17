# 搜索与下载接口文档（前端接入）

本文档供 Web、桌面端等前端应用接入资源搜索与下载服务。

## 1. 接入说明

- 当前服务端地址：`http://125.88.194.226:8000`。
- Swagger 接口页面：`http://125.88.194.226:8000/docs`。
- 数据格式：除文件流下载外，请求体和响应体均为 `application/json`。
- 字符编码：UTF-8。
- **搜索和下载接口均为公开接口，不需要鉴权。**
- 前端请求时不要传 `Authorization`、`X-API-Key` 等鉴权请求头。
- 浏览器跨域访问仍受 CORS 策略约束。推荐通过同域反向代理访问；如果前端与搜索服务不同域，部署方需要放行前端 Origin。

公开接口包括：

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 搜索资源 | `POST` | `/search` |
| 获取临时下载链接 | `POST` | `/download` |
| 下载资源源文件或资源包 | `GET` | `/resources/{resource_id}/download` |
| 下载资源中的单个文件 | `GET` | `/resources/{resource_id}/files/{file_id}/download` |

## 2. 搜索资源

### `POST /search`

根据自然语言及可选过滤条件搜索资源。当前接口采用 Top-K 返回方式，不是页码分页；`total_count` 表示本次实际返回的结果数，不表示库中所有匹配资源的总数。

请求示例：

```http
POST http://125.88.194.226:8000/search
Content-Type: application/json

{
  "query_text": "像素风森林地面瓦片",
  "resource_type": "tileset",
  "format_filter": ["png", "webp"],
  "top_k": 20,
  "similarity_threshold": 0.5,
  "search_mode": "hybrid",
  "bm25_weight": 0.5,
  "enable_reranker": true
}
```

### 请求字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `query_text` | `string` | 是 | - | 搜索文本，支持中文或英文自然语言。建议传入非空文本。 |
| `resource_type` | `string \| null` | 否 | `null` | 资源类型；不传或传 `null` 表示不限类型。可选值见下文。 |
| `format_filter` | `string[] \| null` | 否 | `null` | 文件格式过滤，可传多个值，例如 `["png", "jpg"]`。格式名大小写不敏感，前导点会被忽略。 |
| `top_k` | `integer` | 否 | `10` | 最多返回多少条结果。前端应设置合理上限，常用值为 10～50。 |
| `similarity_threshold` | `number` | 否 | `0.5` | 向量相似度阈值；阈值越高，结果越严格。该参数只直接过滤向量召回结果。 |
| `search_mode` | `string` | 否 | `hybrid` | 搜索模式，只能是 `vector`、`bm25` 或 `hybrid`。 |
| `bm25_weight` | `number` | 否 | `0.5` | 混合搜索中 BM25 的融合权重，范围为 `0`～`1`。仅 `hybrid` 模式有意义。 |
| `enable_reranker` | `boolean \| null` | 否 | `null` | 是否启用重排。`null` 或不传时使用服务端默认配置；当前仅混合搜索会执行重排。 |

搜索模式说明：

| 模式 | 适用场景 |
| --- | --- |
| `hybrid` | 默认且推荐，同时使用语义向量和关键词检索，再进行融合排序。 |
| `vector` | 更看重语义相似度，适合描述性、概念性搜索。 |
| `bm25` | 更看重关键词命中，适合文件名、专有名词或精确词搜索。 |

支持的资源类型：

`pack`、`atlas`、`tiled_map`、`tiled_tileset`、`spine_skeleton`、`spriter`、`dragonbones_skeleton`、`font_file`、`audio_file`、`tileset`、`animation_sequence`、`single_image`、`image`、`model`、`3d_model`、`model_3d`、`design_file`、`other`。

### 成功响应

```json
{
  "results": [
    {
      "resource_id": "res_01HXYZ",
      "resource_type": "tileset",
      "score": 0.0328,
      "primary_preview_url": "https://cdn.example.com/previews/res_01HXYZ/main.webp",
      "other_preview_urls": [
        "https://cdn.example.com/previews/res_01HXYZ/detail.webp"
      ],
      "file_download_url": "https://cdn.example.com/files/res_01HXYZ/tiles.png",
      "package_download_url": "https://cdn.example.com/packages/res_01HXYZ.zip",
      "description_summary": "像素风森林地面与植被瓦片集",
      "file_format": "png",
      "file_size": 245760,
      "status": "completed",
      "preview_available": true,
      "file_count": 12,
      "title": "像素森林瓦片集",
      "source_resource_id": "source-123",
      "vector_score": 0.86,
      "bm25_score": 7.31,
      "rrf_score": 0.0328,
      "reranker_score": 0.91
    }
  ],
  "total_count": 1,
  "suggestion": null
}
```

### 响应字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `results` | `object[]` | 搜索结果数组，已按当前搜索模式的最终相关度排序。 |
| `total_count` | `integer` | 本次实际返回的结果数量，不是全库命中总数。 |
| `suggestion` | `object \| null` | 无结果时可能返回的放宽搜索建议。 |

每条 `results` 结果包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `resource_id` | `string` | 资源唯一 ID，详情和下载接口使用此值。 |
| `resource_type` | `string` | 资源类型。 |
| `title` | `string` | 适合前端展示的资源标题。 |
| `description_summary` | `string` | 资源描述摘要。 |
| `score` | `number` | 当前模式的最终排序分数。不同搜索模式的分数尺度不同，不建议跨模式直接比较。 |
| `primary_preview_url` | `string` | 主预览 URL；没有预览时为空字符串。 |
| `other_preview_urls` | `string[]` | 其他预览 URL。 |
| `preview_available` | `boolean` | 是否有可用预览。 |
| `file_download_url` | `string` | 源对象或主文件的直接下载 URL；没有可下载对象时为空字符串。 |
| `package_download_url` | `string` | 资源包的直接下载 URL；资源没有打包文件时为空字符串。 |
| `file_format` | `string` | 主文件格式，不含前导点；未知时为空字符串。 |
| `file_size` | `integer` | 资源内文件大小合计，单位为字节。 |
| `file_count` | `integer` | 资源内文件数量。 |
| `status` | `string` | 服务端资源处理状态。前端应按普通字符串兼容未知新状态。 |
| `source_resource_id` | `string` | 上游数据源中的资源 ID，可能为空。 |
| `vector_score` | `number` | 向量相似度分数；未参与或无分数时为 `0`。 |
| `bm25_score` | `number` | BM25 关键词分数；未参与或无分数时为 `0`。 |
| `rrf_score` | `number` | RRF 融合分数；未参与或无分数时为 `0`。 |
| `reranker_score` | `number` | 重排模型分数；未执行重排时为 `0`。 |

无结果时的 `suggestion` 示例：

```json
{
  "results": [],
  "total_count": 0,
  "suggestion": {
    "rewrite_queries": ["像素森林 高清", "像素森林 素材"],
    "relaxable_filters": ["resource_type", "format_filter"],
    "suggested_threshold": 0.3,
    "try_cross_type": true
  }
}
```

| 建议字段 | 类型 | 说明 |
| --- | --- | --- |
| `rewrite_queries` | `string[]` | 可供用户点击重试的改写词。 |
| `relaxable_filters` | `string[]` | 建议移除或放宽的过滤字段。 |
| `suggested_threshold` | `number \| null` | 建议使用的相似度阈值。 |
| `try_cross_type` | `boolean` | 是否建议取消资源类型限制。 |

### 前端调用示例

```ts
export interface SearchRequest {
  query_text: string;
  resource_type?: string | null;
  format_filter?: string[] | null;
  top_k?: number;
  similarity_threshold?: number;
  search_mode?: "vector" | "bm25" | "hybrid";
  bm25_weight?: number;
  enable_reranker?: boolean | null;
}

export async function searchResources(baseUrl: string, body: SearchRequest) {
  const response = await fetch(`${baseUrl}/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(`搜索失败：HTTP ${response.status}`);
  }
  return response.json();
}
```

## 3. 下载资源

搜索与下载均不需要鉴权。前端可根据交互方式选择以下方案。

### 方案 A：浏览器直接保存文件（推荐）

```http
GET http://125.88.194.226:8000/resources/{resource_id}/download?kind=source
GET http://125.88.194.226:8000/resources/{resource_id}/download?kind=package
```

| 参数 | 位置 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `resource_id` | path | 是 | - | 搜索结果中的 `resource_id`。 |
| `kind` | query | 否 | `source` | `source` 下载源对象；`package` 下载资源包。 |
| `expire_seconds` | query | 否 | `3600` | 服务端访问对象存储时使用的临时链接有效期，范围 `1`～`86400` 秒。 |

该接口返回文件流，并设置 `Content-Disposition: attachment`。前端可直接设置链接或打开新窗口：

```ts
export function downloadResource(
  baseUrl: string,
  resourceId: string,
  kind: "source" | "package" = "source",
) {
  const url = `${baseUrl}/resources/${encodeURIComponent(resourceId)}/download` +
    `?kind=${encodeURIComponent(kind)}`;
  window.location.assign(url);
}
```

如果指定对象不存在，接口返回 `404`。例如只有源文件、没有资源包时，请求 `kind=package` 会返回 `404`。

### 方案 B：下载资源中的单个文件

```http
GET http://125.88.194.226:8000/resources/{resource_id}/files/{file_id}/download
```

`file_id` 可从资源详情接口 `GET /resources/{resource_id}` 返回的 `files[].file_id` 获取。该接口同样返回带附件响应头的文件流，并支持可选查询参数 `expire_seconds`（`1`～`86400`，默认 `3600`）。

### 方案 C：获取临时下载 URL

```http
POST http://125.88.194.226:8000/download
Content-Type: application/json

{
  "resource_id": "res_01HXYZ",
  "expire_seconds": 3600,
  "return_base64": false
}
```

该接口优先返回资源包 URL，其次返回源对象 URL，再其次返回主文件 URL。

请求字段：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `resource_id` | `string` | 是 | - | 资源唯一 ID。 |
| `expire_seconds` | `integer` | 否 | `3600` | 临时 URL 有效期，单位为秒。建议不超过 `86400`。 |
| `return_base64` | `boolean` | 否 | `false` | 必须为 `false`；服务端已禁用 Base64 下载。传 `true` 返回 HTTP `400`。 |

成功响应：

```json
{
  "download_url": "https://cdn.example.com/packages/res_01HXYZ.zip?...",
  "expires_at": "2026-07-17T15:30:00+00:00",
  "file_name": "res_01HXYZ.zip",
  "file_size": 0,
  "content_type": "application/octet-stream",
  "base64_content": null,
  "error_code": "",
  "error_message": ""
}
```

注意：

- `download_url` 可能是带签名和有效期的临时 URL，前端不要持久化；每次下载前重新获取。
- `expires_at` 为 ISO 8601 时间字符串。
- `file_size` 未知时可能为 `0`，不能据此判断文件为空。
- 资源不存在时当前接口仍可能返回 HTTP `200`，此时 `error_code` 为 `RESOURCE_NOT_FOUND`，`download_url` 为空。前端需要同时检查 HTTP 状态和 `error_code`。

## 4. 错误处理

参数校验失败时，服务使用 FastAPI 标准错误结构，通常返回 HTTP `422`：

```json
{
  "detail": [
    {
      "type": "string_pattern_mismatch",
      "loc": ["body", "search_mode"],
      "msg": "String should match pattern ...",
      "input": "invalid"
    }
  ]
}
```

其他常见状态码：

| 状态码 | 说明 |
| --- | --- |
| `200` | 请求成功；`POST /download` 仍需检查响应内的 `error_code`。 |
| `400` | 下载参数不支持，例如请求 Base64 下载或 `kind` 非法。 |
| `404` | 资源、资源包或单个文件不存在。 |
| `422` | JSON 字段类型、取值范围或枚举值不合法。 |
| `502` | 对象存储或其他上游服务暂时不可用。 |
| `500` | 搜索服务内部错误。 |

前端建议：

- 请求超时、`5xx` 可提示用户稍后重试。
- `422` 应检查请求字段，不建议自动重试同一个请求。
- URL 字段允许为空字符串，展示图片或下载按钮前先判断非空。
- 预览和直接下载 URL 可能过期；页面停留较久后可重新搜索或重新调用下载接口。

## 5. 最小接入流程

1. 调用 `POST /search` 获取结果。
2. 使用 `title`、`description_summary`、`primary_preview_url` 展示资源卡片。
3. 用户点击下载时，使用 `resource_id` 请求 `GET /resources/{resource_id}/download`。
4. 资源包优先的场景传 `kind=package`；如果服务返回 `404`，可按产品需要回退到 `kind=source`。
