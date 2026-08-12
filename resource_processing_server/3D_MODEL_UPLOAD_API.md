# 3D 模型上传与加工接口文档

本文档供桌面端、Web 后端、编辑器插件及其他受信任客户端接入 3D 模型上传与加工服务。

## 1. 接口概览

当前上传流程分为两步：

1. 客户端先通过 S3 兼容协议把模型源文件、依赖文件和可选预览图上传到对象存储。
2. 客户端向资源加工服务器提交 JSON manifest，由服务器生成或复用预览和描述，并写入搜索服务。

> 当前资源加工服务器不接收 `multipart/form-data` 文件，也没有申请预签名上传 URL 的接口。`POST /processing-jobs` 只接收对象存储引用。接入方需要由部署方分配受限的对象存储写入凭据，或先补充“预签名上传 URL”接口。不要把长期对象存储密钥写入浏览器、移动端或公开发行的软件中。

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 健康检查 | `GET` | `/health` |
| 提交单个模型 | `POST` | `/processing-jobs` |
| 批量提交模型 | `POST` | `/processing-jobs/batch` |
| 查询单个任务 | `GET` | `/processing-jobs/{job_id}` |
| 批量查询任务 | `POST` | `/processing-jobs/status` |
| 重试任务 | `POST` | `/processing-jobs/{job_id}/retry` |

- 正式上传加工服务地址：`http://125.88.194.226:9000`
- 上传加工服务 Swagger：`http://125.88.194.226:9000/docs`
- 搜索与下载服务地址：`http://125.88.194.226:8000`
- 请求和响应格式：`application/json; charset=utf-8`

`8000` 端口是“数字资源语义检索服务”，用于搜索和下载，不提供 `/processing-jobs`；3D 模型提交必须调用同一服务器的 `9000` 端口。客户端 ID、API Key、对象存储配置和可写 key 前缀由部署方提供。

## 2. 鉴权

除 `/health` 外，以上接口均需携带客户端身份和 API Key：

```http
X-Client-Id: your-client-id
X-API-Key: your-api-key
```

API Key 也可以放在以下任一种请求头中：

```http
Authorization: Bearer your-api-key
Authorization: ApiKey your-api-key
```

`X-Client-Id` 始终必填。API Key 只允许访问相同 `X-Client-Id` 创建的任务。

## 3. 模型文件要求

### 3.1 资源类型

新客户端统一使用：

```json
"resource_type": "model"
```

服务端也兼容历史值 `3d_model` 和 `model_3d`，但不建议新客户端继续使用。

### 3.2 单文件模型

自包含模型（例如 `.glb`）可以直接作为 `source_object` 上传。

推荐对象 key：

```text
<允许的前缀>/<client-id>/files/<client-resource-id>/<file-name>
```

3D 资源桶当前只允许 `resource-3d/` 前缀。例如：

```text
resource-3d/model-editor/files/chair-001/chair.glb
```

### 3.3 多文件模型

OBJ、GLTF 等模型可能依赖 `.mtl`、`.bin` 和纹理图片。此类资源必须把完整目录打成一个 ZIP，并将 ZIP 作为唯一的 `source_object` 上传：

```text
chair.zip
├── chair.obj
├── chair.mtl
└── textures/
    ├── basecolor.png
    └── normal.png
```

同时在 `file_structure.entries` 中列出 ZIP 内需要使用的所有文件，并且恰好指定一个主模型文件 `is_primary: true`。ZIP 内路径必须使用 `/`，不得包含绝对路径、空路径、`.` 或 `..`。

服务端默认 ZIP 安全限制：

| 限制 | 默认值 |
| --- | ---: |
| ZIP 成员数 | 512 |
| 单个解压文件大小 | 256 MiB |
| 总解压大小 | 1 GiB |
| 最大压缩比 | 100:1 |

这些值可由部署配置调整。

### 3.4 预览图

- 可以在 `previews` 中提供已上传的 PNG、WEBP 或 GIF；第一张主预览使用 `role: "primary"`。
- 不提供预览时，服务端会尝试生成预览。
- 当前内置的 3D 自动预览明确支持 FBX；其他格式的自动预览没有稳定保证。因此上传 GLB、GLTF、OBJ 等格式时，客户端应同时提供有效预览图，否则任务可能在 `preview` 阶段失败。
- 提供的预览图也必须先上传到与服务端共享的对象存储。

## 4. 上传对象存储

对象存储使用 S3 兼容协议。部署方需提供：

| 配置 | 当前示例值/说明 |
| --- | --- |
| Endpoint | `https://cos.ap-guangzhou.myqcloud.com` |
| Region | `ap-guangzhou` |
| Bucket | `game-ai-studio-resource3d-1252100362` |
| Signature | S3 v4 |
| Addressing style | virtual-hosted |
| Access key / Secret key | 由部署方单独安全分配 |
| CDN domain | `https://gameai-studio-3d.seasungame.com` |
| Storage profile ID | `game-ai-studio-resource3d-1252100362` |
| 允许的 object key 前缀 | `resource-3d/` |

Python 上传示例：

```python
from pathlib import Path
import hashlib

import boto3
from botocore.config import Config

path = Path("chair.glb")
object_key = "resource-3d/model-editor/files/chair-001/chair.glb"

s3 = boto3.client(
    "s3",
    endpoint_url="https://cos.ap-guangzhou.myqcloud.com",
    region_name="ap-guangzhou",
    aws_access_key_id="<OBJECT_STORAGE_ACCESS_KEY>",
    aws_secret_access_key="<OBJECT_STORAGE_SECRET_KEY>",
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

s3.upload_file(
    str(path),
    "game-ai-studio-resource3d-1252100362",
    object_key,
    ExtraArgs={"ContentType": "model/gltf-binary"},
)

checksum = hashlib.md5(path.read_bytes()).hexdigest()
print({"object_key": object_key, "size": path.stat().st_size, "checksum": checksum})
```

上传完成后再提交 manifest。服务端默认会用 `HEAD` 检查对象是否存在。

## 5. 提交单文件模型

### `POST /processing-jobs`

最小请求示例：

```http
POST http://125.88.194.226:9000/processing-jobs
Content-Type: application/json
X-Client-Id: model-editor
X-API-Key: <API_KEY>

{
  "request_id": "model-editor:chair-001:v1",
  "client_resource_id": "chair-001",
  "resource_type": "model",
  "source_object": {
    "storage_profile_id": "game-ai-studio-resource3d-1252100362",
    "object_key": "resource-3d/model-editor/files/chair-001/chair.glb",
    "file_name": "chair.glb",
    "file_format": "glb",
    "size": 12582912,
    "checksum": "4b3b0d4f8f1c7a3e5e938f9b54d71c32"
  },
  "file_structure": {
    "source": "client",
    "state": "complete",
    "source_object_checksum": "4b3b0d4f8f1c7a3e5e938f9b54d71c32",
    "entry_count": 1,
    "total_size": 12582912,
    "entries": [
      {
        "path": "chair.glb",
        "name": "chair.glb",
        "type": "file",
        "size": 12582912,
        "format": "glb",
        "checksum": "4b3b0d4f8f1c7a3e5e938f9b54d71c32",
        "is_primary": true
      }
    ]
  },
  "previews": [
    {
      "role": "primary",
      "storage_profile_id": "game-ai-studio-resource3d-1252100362",
      "object_key": "resource-3d/model-editor/previews/chair-001/primary.webp",
      "width": 512,
      "height": 512,
      "size": 86420,
      "checksum": "ab10988c54aaac2c9d238043c13f7421"
    }
  ],
  "description_context": {
    "title": "现代木椅",
    "category": "家具",
    "tags": ["椅子", "木质", "室内"],
    "source": "model-editor"
  },
  "client_metadata": {
    "display_title": "现代木椅",
    "source": "model-editor",
    "source_resource_id": "chair-001"
  }
}
```

`file_structure` 可以省略，服务器会下载 `source_object` 并扫描生成。正式客户端仍建议传入，因为它能更早发现文件结构问题，也能避免服务端为扫描重复下载源对象。

### 请求字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `request_id` | `string` | 否 | `""` | 客户端请求追踪 ID。建议固定为“客户端 + 资源 ID + 版本”。它本身不是独立的幂等键。 |
| `client_resource_id` | `string` | 是 | - | 客户端侧稳定且唯一的资源 ID；同一客户端内不可为空。更新同一资源时保持不变。 |
| `resource_type` | `string` | 是 | - | 新客户端传 `model`。 |
| `source_object` | `ObjectRef` | 是 | - | 已上传的单文件模型或 ZIP 引用。 |
| `file_structure` | `FileStructure \| null` | 否 | `null` | 模型文件结构；多文件 ZIP 强烈建议填写。 |
| `package_object` | `ObjectRef \| null` | 否 | `null` | 资源所属上层资源包的下载对象，不是多文件模型自身的 ZIP；普通模型不要填写。 |
| `previews` | `PreviewRef[]` | 否 | `[]` | 已上传预览图列表；非 FBX 模型强烈建议填写。 |
| `description` | `Description \| null` | 否 | `null` | 客户端已生成的描述。填写后服务端直接使用，不再生成描述。 |
| `description_context` | `object \| null` | 否 | `null` | 供服务端生成描述使用的标题、标签、来源等上下文。 |
| `client_metadata` | `object` | 否 | `{}` | 原样带入最终搜索记录的客户端元数据。 |
| `classification` | `Classification \| null` | 否 | `null` | 客户端已有的用途分类。 |
| `options` | `object` | 否 | 默认值 | 兼容旧客户端的字段；当前服务端忽略客户端策略并自行决定处理流程，不建议传。 |

`ObjectRef` 字段：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `storage_profile_id` | `string` | 建议 | `""` | 对象存储配置 ID；空值使用服务端默认 profile。跨客户端接入建议明确填写。 |
| `object_key` | `string` | 是 | - | Bucket 内对象 key，不能以 `/` 开头、包含反斜杠或 `..`，且必须符合服务端允许前缀。 |
| `file_name` | `string` | 建议 | `""` | 下载到服务端后的文件名；ZIP 必须以 `.zip` 结尾。 |
| `file_format` | `string` | 建议 | `""` | 不带点的扩展名，如 `glb`、`fbx`、`zip`。 |
| `size` | `integer` | 否 | `0` | 对象字节数。 |
| `checksum` | `string` | 建议 | `""` | 当前客户端采用文件 MD5 小写十六进制。用于指纹和结构一致性校验。 |

`FileStructureEntry` 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `path` | `string` | 是 | ZIP 内相对路径；单文件可传文件名。 |
| `name` | `string` | 是 | 文件名，不得为空。 |
| `type` | `string` | 否 | 当前固定为 `file`。 |
| `size` | `integer` | 否 | 文件字节数，不能为负数。 |
| `format` | `string` | 否 | 不带点的扩展名。 |
| `checksum` | `string` | 否 | 文件 MD5。 |
| `is_primary` | `boolean` | 否 | 是否为主模型文件。未指定时，服务端会把第一项设为主文件。 |

成功响应：

```json
{
  "job_id": "job_58f12d47a63c4b82",
  "state": "queued",
  "resource_fingerprint": "a94d7b197fd5f49a..."
}
```

提交成功时当前接口返回 HTTP `200`，但这并不表示加工完成；业务状态通常为 `queued`。客户端必须保存 `job_id` 并轮询任务状态。

## 6. 提交多文件模型

多文件模型的 `source_object` 指向 ZIP，`file_structure.entries` 指向 ZIP 内文件：

```json
{
  "request_id": "model-editor:chair-obj-001:v1",
  "client_resource_id": "chair-obj-001",
  "resource_type": "model",
  "source_object": {
    "storage_profile_id": "game-ai-studio-resource3d-1252100362",
    "object_key": "resource-3d/model-editor/files/chair-obj-001/source.zip",
    "file_name": "source.zip",
    "file_format": "zip",
    "size": 21810380,
    "checksum": "63edc17a128e6c3e08dc95cc9a30a821"
  },
  "file_structure": {
    "source": "client",
    "state": "complete",
    "source_object_checksum": "63edc17a128e6c3e08dc95cc9a30a821",
    "entry_count": 4,
    "total_size": 30408722,
    "entries": [
      {"path": "chair.obj", "name": "chair.obj", "size": 8321450, "format": "obj", "checksum": "<MD5>", "is_primary": true},
      {"path": "chair.mtl", "name": "chair.mtl", "size": 4812, "format": "mtl", "checksum": "<MD5>", "is_primary": false},
      {"path": "textures/basecolor.png", "name": "basecolor.png", "size": 12041220, "format": "png", "checksum": "<MD5>", "is_primary": false},
      {"path": "textures/normal.png", "name": "normal.png", "size": 10041240, "format": "png", "checksum": "<MD5>", "is_primary": false}
    ]
  },
  "previews": [
    {
      "role": "primary",
      "storage_profile_id": "game-ai-studio-resource3d-1252100362",
      "object_key": "resource-3d/model-editor/previews/chair-obj-001/primary.webp",
      "width": 512,
      "height": 512
    }
  ],
  "description_context": {
    "title": "现代木椅",
    "tags": ["chair", "wood", "furniture"]
  }
}
```

`entry_count` 必须等于 `entries` 数量，`total_size` 必须等于所有 entry 的 `size` 之和，`file_structure.source_object_checksum` 与 `source_object.checksum` 同时非空时必须一致。

## 7. 查询任务状态

### `GET /processing-jobs/{job_id}`

```http
GET http://125.88.194.226:9000/processing-jobs/job_58f12d47a63c4b82
X-Client-Id: model-editor
X-API-Key: <API_KEY>
```

响应：

```json
{
  "job_id": "job_58f12d47a63c4b82",
  "state": "completed",
  "client_resource_id": "chair-001",
  "search_resource_id": "res_79cc4ec2a13b4a49",
  "steps": [
    {"name": "file_structure_provided", "state": "completed", "duration_ms": 0, "error": ""},
    {"name": "preview", "state": "completed", "duration_ms": 185, "error": ""},
    {"name": "description", "state": "completed", "duration_ms": 1240, "error": ""},
    {"name": "search_upsert", "state": "completed", "duration_ms": 86, "error": ""}
  ],
  "error": null
}
```

任务状态：

| 状态 | 含义 | 客户端处理 |
| --- | --- | --- |
| `queued` | 已入队 | 继续轮询 |
| `validating` | 校验和下载源文件 | 继续轮询 |
| `previewing` | 处理预览 | 继续轮询 |
| `describing` | 生成或复用描述 | 继续轮询 |
| `submitting` | 写入搜索服务 | 继续轮询 |
| `completed` | 完成 | 保存 `search_resource_id` |
| `failed` | 失败 | 读取 `error`，修正数据或调用重试接口 |
| `cancelled` | 已取消 | 重新提交或调用重试接口 |

建议轮询间隔 2 秒。长任务的客户端总超时建议至少 1 小时。

### 批量查询

```http
POST /processing-jobs/status
Content-Type: application/json
X-Client-Id: model-editor
X-API-Key: <API_KEY>

{
  "job_ids": ["job_58f12d47a63c4b82", "job_6c249c7372e84c42"]
}
```

单次最多查询 1000 个去重后的非空任务 ID：

```json
{
  "jobs": [
    {
      "job_id": "job_58f12d47a63c4b82",
      "state": "completed",
      "client_resource_id": "chair-001",
      "search_resource_id": "res_79cc4ec2a13b4a49",
      "error": null
    }
  ],
  "missing_job_ids": ["job_6c249c7372e84c42"]
}
```

## 8. 批量提交

### `POST /processing-jobs/batch`

```json
{
  "request_id": "model-editor:batch-20260812-01",
  "manifests": [
    {
      "request_id": "model-editor:chair-001:v1",
      "client_resource_id": "chair-001",
      "resource_type": "model",
      "source_object": {
        "storage_profile_id": "game-ai-studio-resource3d-1252100362",
        "object_key": "resource-3d/model-editor/files/chair-001/chair.glb",
        "file_name": "chair.glb",
        "file_format": "glb"
      },
      "previews": [
        {
          "role": "primary",
          "storage_profile_id": "game-ai-studio-resource3d-1252100362",
          "object_key": "resource-3d/model-editor/previews/chair-001/primary.webp"
        }
      ]
    }
  ]
}
```

响应：

```json
{
  "batch_id": "batch_8b25be7da96d4de5",
  "jobs": [
    {
      "job_id": "job_58f12d47a63c4b82",
      "client_resource_id": "chair-001",
      "state": "queued",
      "resource_fingerprint": "a94d7b197fd5f49a..."
    }
  ]
}
```

批量接口当前没有显式的 manifest 数量上限。客户端仍应分批提交，建议每批 20～50 个，并使用批量状态查询降低请求量。

## 9. 重试、幂等与更新

### 重试失败任务

```http
POST /processing-jobs/job_58f12d47a63c4b82/retry
X-Client-Id: model-editor
X-API-Key: <API_KEY>
```

响应结构与提交单个模型一致。

### 幂等语义

- 生产环境按 `X-Client-Id + client_resource_id + 完整 manifest 指纹` 去重。
- 完全相同的 manifest 重复提交会返回已有任务；失败或取消的已有任务会重新排队。
- `request_id` 是 manifest 指纹的一部分，但不是服务端单独使用的幂等键。重试相同请求时必须保持整个 manifest（包括 `request_id`）不变。
- 修改文件对象、校验和、预览、描述或其他 manifest 字段后会创建新任务。
- 更新同一逻辑资源时保持 `client_resource_id` 不变。加工完成后，搜索记录会按该 ID 更新。
- 网络超时后不要立即生成新请求 ID；先用原请求体重新提交并取得任务 ID。

## 10. 错误响应

| HTTP 状态码 | 含义 | 常见原因 |
| --- | --- | --- |
| `200` | 请求成功 | 已创建、找到或重新排队任务 |
| `401` | 鉴权失败 | 缺少 `X-Client-Id`、API Key 缺失或不匹配 |
| `404` | 任务不存在 | `job_id` 错误，或任务属于另一个客户端 |
| `422` | 请求校验失败 | 必填字段为空、file structure 不一致、resource type 不可加工、对象 key 非法 |
| `500` | 服务内部错误 | 数据库、对象存储或下游服务异常 |
| `503` | 服务不可用 | 健康检查发现加工数据库不可用 |

FastAPI 参数校验错误示例：

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "source_object", "object_key"],
      "msg": "Value error, must not be blank",
      "input": ""
    }
  ]
}
```

业务校验错误可能返回字符串 detail：

```json
{
  "detail": "resource_type 'pack' is not submitted to processing"
}
```

提交请求可对网络错误以及 HTTP `429`、`502`、`503`、`504` 做指数退避重试；其他 `4xx` 应先修正请求，不要盲目重试。

## 11. 完整 Python 客户端示例

下面示例假设模型和预览已经上传到对象存储：

```python
import time
import requests

BASE_URL = "http://125.88.194.226:9000"
HEADERS = {
    "X-Client-Id": "model-editor",
    "X-API-Key": "<API_KEY>",
}

manifest = {
    "request_id": "model-editor:chair-001:v1",
    "client_resource_id": "chair-001",
    "resource_type": "model",
    "source_object": {
        "storage_profile_id": "game-ai-studio-resource3d-1252100362",
        "object_key": "resource-3d/model-editor/files/chair-001/chair.glb",
        "file_name": "chair.glb",
        "file_format": "glb",
        "size": 12582912,
        "checksum": "4b3b0d4f8f1c7a3e5e938f9b54d71c32",
    },
    "previews": [{
        "role": "primary",
        "storage_profile_id": "game-ai-studio-resource3d-1252100362",
        "object_key": "resource-3d/model-editor/previews/chair-001/primary.webp",
        "width": 512,
        "height": 512,
    }],
    "description_context": {
        "title": "现代木椅",
        "tags": ["椅子", "家具", "木质"],
    },
}

with requests.Session() as session:
    created = session.post(
        f"{BASE_URL}/processing-jobs",
        headers=HEADERS,
        json=manifest,
        timeout=60,
    )
    created.raise_for_status()
    job_id = created.json()["job_id"]

    while True:
        response = session.get(
            f"{BASE_URL}/processing-jobs/{job_id}",
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        job = response.json()
        if job["state"] == "completed":
            print("uploaded:", job["search_resource_id"])
            break
        if job["state"] in {"failed", "cancelled"}:
            raise RuntimeError(job.get("error") or f"job {job['state']}")
        time.sleep(2)
```

## 12. 接入前需要部署方提供的内容

1. 资源加工服务器的正式 Base URL。
2. 分配给客户端的 `X-Client-Id` 和 API Key。
3. 对象存储的 profile ID、Bucket、Endpoint、Region 和可写 key 前缀。
4. 适合客户端形态的上传授权方式。后端或内网受信任工具可使用受限 S3 凭据；浏览器、移动端和公开发行客户端应使用短期凭据或预签名上传 URL。
5. 是否已部署支持目标模型格式的预览渲染器。未确认时，客户端应自行生成并上传预览图。

当前代码库只实现了对象存储直传和 manifest 提交。若目标是给不受信任的外部客户端开放上传，建议下一步在服务端增加“初始化上传 -> 返回预签名 URL -> 确认上传 -> 创建加工任务”的接口，避免分发长期对象存储密钥。
