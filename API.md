# BiliTranscript 提取 API 调用说明

本文档适用于 BiliTranscript `1.0.0` 的局域网提取 API。该 API 供桌面软件、后端服务和自动化脚本调用 BiliTranscript 的文稿提取能力。

API 采用异步任务模型：先提交一个视频，取得任务 ID；再查询任务状态；任务成功后读取 JSON、Markdown、纯文本或 SRT 结果。每个任务会提取该视频的全部分P。

## 1. 启动 API 服务

1. 启动 BiliTranscript；提取 API 默认随软件启动。
2. 打开侧栏“设置”→“提取 API”。
3. 第一次启动时，软件会自动生成 API Key。请妥善保存，不要发给不可信的人。
4. 确认服务显示“运行中”并复制 API Key；默认端口是 `8766`。
5. 如需停止服务、更换端口或关闭随软件自动运行，在同一页修改后点击“应用”。

设置窗口会显示可直接调用的地址：

- 同一台电脑：`http://127.0.0.1:8766`
- 局域网其他设备：例如 `http://192.168.1.20:8766`

`http://0.0.0.0:8766` 是服务的监听地址，不是客户端应使用的访问地址。关闭 BiliTranscript 后，API 服务和所有内存中的任务、结果都会消失。

先进行健康检查：

```powershell
curl.exe http://127.0.0.1:8766/health
```

正常响应：

```json
{
  "status": "ok",
  "service": "BiliTranscript",
  "version": "1.0.0"
}
```

## 2. 鉴权与安全

除 `GET /health` 和 `GET /openapi.json` 外，所有接口都必须携带以下请求头：

```http
Authorization: Bearer YOUR_API_KEY
```

示例：

```powershell
curl.exe http://127.0.0.1:8766/v1/capabilities `
  -H "Authorization: Bearer YOUR_API_KEY"
```

重新生成并保存 API Key 后，旧 Key 会立即失效。API 不会在健康检查、能力响应或错误信息中返回提取 API Key、上游 ASR 地址或上游 ASR 密钥。

该服务只适合可信局域网：

- 服务使用 HTTP，不提供内置 HTTPS。不要把端口直接映射到公网。
- 服务不开放 CORS。浏览器网页不应直接保存 API Key 并调用本接口，应由网页自己的后端转发。
- 其他设备无法访问时，需要在 Windows 防火墙中允许 `BiliTranscript.exe` 的专用网络访问。软件不会自动修改防火墙规则。

## 3. 基本调用流程

### 第一步：提交提取任务

```http
POST /v1/extractions
Content-Type: application/json
Authorization: Bearer YOUR_API_KEY

{"source":"BV1xxxxxxxxx"}
```

`source` 支持 BV 号、av 号、完整 B站视频链接和 `b23.tv` 短链接。请求体必须且只能识别出一个视频，不能在一次请求中提交多个视频。

成功后返回 HTTP `202 Accepted`：

```json
{
  "id": "0123456789abcdef0123456789abcdef",
  "status": "queued",
  "source": "BV1xxxxxxxxx",
  "progress": 0,
  "message": "等待处理",
  "created_at": "2026-07-27T14:30:00+08:00",
  "started_at": null,
  "finished_at": null,
  "cancel_requested": false,
  "has_issues": false,
  "settings": {
    "mode": "auto",
    "browser_ai": true,
    "asr_backend": "openai-compatible",
    "asr_model": "mimo-asr",
    "language": "zh"
  },
  "status_url": "/v1/extractions/0123456789abcdef0123456789abcdef",
  "result_url": "/v1/extractions/0123456789abcdef0123456789abcdef/result"
}
```

`status_url` 和 `result_url` 是相对路径，调用时需要在前面加上服务地址。

### 第二步：查询任务状态

```http
GET /v1/extractions/{id}
Authorization: Bearer YOUR_API_KEY
```

建议每隔 1 秒查询一次，直到 `status` 进入以下终态之一：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已进入队列，等待执行 |
| `running` | 正在读取视频或提取文稿 |
| `succeeded` | 提取任务完成，可以读取结果 |
| `failed` | 整个任务失败，状态响应中会包含 `error` |
| `cancelled` | 任务已取消 |

状态响应中的主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 32 位小写十六进制任务 ID |
| `status` | string | 当前任务状态 |
| `progress` | integer | `0` 到 `100` 的进度估计 |
| `message` | string | 供用户阅读的当前提示，不应作为程序判断条件 |
| `created_at` | string | 带时区的 ISO 8601 提交时间 |
| `started_at` | string/null | 开始执行时间 |
| `finished_at` | string/null | 进入终态的时间 |
| `cancel_requested` | boolean | 是否已收到取消请求 |
| `has_issues` | boolean | 是否存在未成功提取的分P |
| `settings` | object | 任务提交瞬间保存的非敏感提取设置 |
| `error` | string | 仅任务失败时出现的失败原因 |

一个视频的部分分P失败、其他分P成功时，任务仍会是 `succeeded`，同时 `has_issues` 为 `true`。具体失败分P见 JSON 结果的 `issues`，或 Markdown 结果末尾的“未提取的分P”。

### 第三步：获取结果

仅当任务状态为 `succeeded` 时调用：

```http
GET /v1/extractions/{id}/result?format=json&timestamps=false
Authorization: Bearer YOUR_API_KEY
```

查询参数：

| 参数 | 可选值 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `format` | `json`、`markdown`、`text`、`srt` | `json` | 返回格式 |
| `timestamps` | `true`、`false`、`1`、`0` | `false` | Markdown 或纯文本是否在每段前显示时间戳 |

输出格式与响应类型：

| `format` | `Content-Type` | 时间戳行为 |
| --- | --- | --- |
| `json` | `application/json; charset=utf-8` | `segments` 始终包含数字形式的 `start` 和 `end`；`timestamps` 不改变 JSON |
| `markdown` | `text/markdown; charset=utf-8` | `timestamps=true` 时添加 `[00:12]` 形式的时间戳 |
| `text` | `text/plain; charset=utf-8` | `timestamps=true` 时添加 `[00:12]` 形式的时间戳 |
| `srt` | `application/x-subrip; charset=utf-8` | 始终包含 SRT 时间码 |

如果任务还在排队、运行、失败或已取消，该接口返回 HTTP `409 result_not_ready`。

## 4. 接口一览

| 方法 | 路径 | 鉴权 | 成功状态 | 用途 |
| --- | --- | --- | --- | --- |
| `GET` | `/health` | 否 | `200` | 健康检查 |
| `GET` | `/openapi.json` | 否 | `200` | 获取 OpenAPI 3.1 规范 |
| `GET` | `/v1/capabilities` | 是 | `200` | 获取当前非敏感配置和服务限制 |
| `POST` | `/v1/extractions` | 是 | `202` | 提交单个视频提取任务 |
| `GET` | `/v1/extractions/{id}` | 是 | `200` | 查询状态和进度 |
| `GET` | `/v1/extractions/{id}/result` | 是 | `200` | 获取提取结果 |
| `POST` | `/v1/extractions/{id}/cancel` | 是 | `202` 或 `200` | 取消任务 |

### `GET /v1/capabilities`

示例响应：

```json
{
  "service": "BiliTranscript",
  "version": "1.0.0",
  "settings": {
    "mode": "auto",
    "browser_ai": true,
    "asr_backend": "openai-compatible",
    "asr_model": "mimo-asr",
    "language": "zh"
  },
  "all_parts": true,
  "result_formats": ["json", "markdown", "text", "srt"],
  "max_concurrent_jobs": 3,
  "max_pending_jobs": 32
}
```

`settings.mode` 可能为：

| 值 | 桌面界面选项 |
| --- | --- |
| `auto` | 智能提取 |
| `public` | 只用公开字幕 |
| `anonymous` | 只用匿名接口 |
| `browser` | 只用登录浏览器 |
| `asr` | 只用 ASR |

`settings.asr_backend` 可能为 `auto`、`faster-whisper`、`funasr`、`openai-whisper` 或 `openai-compatible`。

API 请求不能覆盖这些设置，也不能选择单独分P。每次提交时，BiliTranscript 会保存桌面界面当时的提取方式、ASR 后端、模型和上游 ASR 配置，之后修改界面不会影响已经提交的任务。敏感的上游 ASR 地址和密钥不会出现在响应中。

### `POST /v1/extractions`

请求必须满足以下条件：

- `Content-Type` 必须是 `application/json`。
- 请求体必须是 UTF-8 JSON 对象。
- 只允许 `source` 字段，传入 `mode`、`format` 等额外字段会被拒绝。
- `source` 必须是非空字符串，最长 2048 个字符。
- 整个请求体最大 16 KiB。
- `source` 中必须且只能识别出一个 B站视频。

如需批量提取，由调用方为每个视频分别提交一个任务。服务最多同时执行 3 个任务，最多允许 32 个未结束任务；队列已满时返回 HTTP `429 queue_full`。

### `POST /v1/extractions/{id}/cancel`

取消接口不需要请求体：

```powershell
curl.exe -X POST `
  "http://127.0.0.1:8766/v1/extractions/0123456789abcdef0123456789abcdef/cancel" `
  -H "Authorization: Bearer YOUR_API_KEY"
```

- 排队中或运行中的任务：返回 HTTP `202`。运行中的工作会协作式停止，状态可能短暂保持 `running`，同时 `cancel_requested` 为 `true`。
- 已经结束的任务：返回 HTTP `200` 和当前状态。
- 重复取消是安全的，不会产生新的任务或错误。

## 5. JSON 结果结构

`format=json` 的响应沿用 BiliTranscript 的 `TranscriptBundle` 结构，并增加顶层 `has_issues`：

```json
{
  "schema_version": 1,
  "created_at": "2026-07-27T14:35:20+08:00",
  "video": {
    "bvid": "BV1xxxxxxxxx",
    "aid": 123456789,
    "title": "视频标题",
    "owner": "UP 主名称",
    "duration": 321,
    "url": "https://www.bilibili.com/video/BV1xxxxxxxxx/"
  },
  "parts": [
    {
      "page": 1,
      "cid": 987654321,
      "title": "第一部分",
      "duration": 321,
      "source": "B站 AI 字幕",
      "language": "zh-CN",
      "segments": [
        {
          "start": 0.0,
          "end": 2.4,
          "text": "第一段文稿"
        }
      ],
      "text": "第一段文稿"
    }
  ],
  "issues": [],
  "has_issues": false
}
```

`parts` 只包含成功提取的分P。`issues` 的每一项包含：

```json
{
  "page": 2,
  "title": "第二部分",
  "message": "没有可用字幕，ASR 也不可用"
}
```

调用方应以字段含义为准，不要依赖数组一定非空，也不要根据中文 `message` 文案编写业务分支。

## 6. 错误响应

所有 API 错误都使用统一 JSON 格式，不返回 Python 堆栈：

```json
{
  "error": {
    "code": "invalid_source",
    "message": "source 中没有可识别的 B站视频"
  }
}
```

主要错误码：

| HTTP 状态 | `error.code` | 说明 |
| --- | --- | --- |
| `400` | `invalid_content_type` | 提交请求没有使用 `application/json` |
| `400` | `missing_content_length` | 缺少 `Content-Length` |
| `400` | `invalid_content_length` | `Content-Length` 无效 |
| `400` | `invalid_json` | 请求体不是有效的 UTF-8 JSON |
| `400` | `invalid_request` | JSON 类型或字段不符合要求 |
| `400` | `invalid_source` | 没有识别出视频、识别出多个视频或输入过长 |
| `400` | `invalid_query` | 查询参数未知、重复或用于不支持查询参数的接口 |
| `400` | `invalid_format` | `format` 不在支持列表中 |
| `400` | `invalid_timestamps` | `timestamps` 不是支持的布尔值 |
| `401` | `unauthorized` | Bearer API Key 缺失或错误 |
| `404` | `job_not_found` | 任务不存在或结果已过期 |
| `404` | `not_found` | 路径不存在或任务 ID 格式不正确 |
| `405` | `method_not_allowed` | HTTP 方法不受支持 |
| `409` | `result_not_ready` | 任务尚未成功完成，不能获取结果 |
| `413` | `request_too_large` | 请求体超过 16 KiB |
| `429` | `queue_full` | 已有 32 个未结束任务 |
| `500` | `internal_error` | API 内部错误 |
| `503` | `service_stopping` | 服务正在停止，不再接受新任务 |

建议调用方按以下方式处理：

- `400`、`401`：修正请求或密钥后再提交，不要原样自动重试。
- `404`：确认任务 ID；成功结果可能已超过保留时间。
- `409`：先查询任务状态。只有 `queued` 或 `running` 才应继续轮询。
- `429`：等待已有任务结束后再提交。响应不包含固定的 `Retry-After`，建议退避 2 到 10 秒。
- `500`、网络断开：可有限次数重试状态查询；重新提交任务前应先确认原任务是否已经创建，避免重复处理。

## 7. PowerShell 完整示例

下面的脚本会提交任务、每秒查询状态，并将成功结果保存为 Markdown：

```powershell
$BaseUrl = "http://127.0.0.1:8766"
$ApiKey = "YOUR_API_KEY"
$Headers = @{ Authorization = "Bearer $ApiKey" }

$Body = @{ source = "BV1xxxxxxxxx" } | ConvertTo-Json -Compress
$Job = Invoke-RestMethod `
  -Method Post `
  -Uri "$BaseUrl/v1/extractions" `
  -Headers $Headers `
  -ContentType "application/json; charset=utf-8" `
  -Body $Body

Write-Host "任务 ID: $($Job.id)"

do {
  Start-Sleep -Seconds 1
  $State = Invoke-RestMethod `
    -Method Get `
    -Uri "$BaseUrl/v1/extractions/$($Job.id)" `
    -Headers $Headers
  Write-Host "$($State.status) $($State.progress)% $($State.message)"
} while ($State.status -in @("queued", "running"))

if ($State.status -eq "succeeded") {
  Invoke-WebRequest `
    -Method Get `
    -Uri "$BaseUrl/v1/extractions/$($Job.id)/result?format=markdown&timestamps=true" `
    -Headers $Headers `
    -OutFile ".\transcript.md"
  Write-Host "已保存到 transcript.md；部分失败: $($State.has_issues)"
} else {
  throw "提取未成功：$($State.status) $($State.error)"
}
```

只读取 JSON 结果：

```powershell
$Result = Invoke-RestMethod `
  -Uri "$BaseUrl/v1/extractions/$($Job.id)/result?format=json" `
  -Headers $Headers

$Result.parts | ForEach-Object {
  Write-Host "P$($_.page) $($_.title): $($_.text.Length) 个字符"
}
```

## 8. Python 标准库完整示例

该示例不需要安装第三方库：

```python
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8766"
API_KEY = "YOUR_API_KEY"


def request(path, method="GET", payload=None):
    body = None
    headers = {"Authorization": f"Bearer {API_KEY}"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        BASE_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        return urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 请求失败：HTTP {exc.code} {detail}") from exc


with request(
    "/v1/extractions",
    method="POST",
    payload={"source": "BV1xxxxxxxxx"},
) as response:
    job = json.load(response)

print("任务 ID:", job["id"])

while True:
    with request(f"/v1/extractions/{job['id']}") as response:
        state = json.load(response)
    print(state["status"], state["progress"], state["message"])
    if state["status"] not in {"queued", "running"}:
        break
    time.sleep(1)

if state["status"] != "succeeded":
    raise RuntimeError(f"提取未成功：{state}")

result_path = f"/v1/extractions/{job['id']}/result?format=markdown&timestamps=true"
with request(result_path) as response:
    Path("transcript.md").write_bytes(response.read())

print("已保存到 transcript.md；部分失败:", state["has_issues"])
```

仓库中还提供了一个可直接运行的端到端验证程序。它会依次检查健康状态和 OpenAPI、验证鉴权、提交任务、轮询状态、校验 JSON 结构，并下载 Markdown：

```powershell
$env:BILITRANSCRIPT_API_KEY = "YOUR_API_KEY"
python .\tools\api_smoke_test.py "BV1xxxxxxxxx" --output ".\api-smoke-result.md"
Remove-Item Env:\BILITRANSCRIPT_API_KEY
```

程序最终输出 `PASS: the extraction API completed the full workflow.` 才表示完整流程通过；失败时退出码为非零，并显示对应的 HTTP 状态或 API 错误码。

## 9. 并发、保留时间与超时

- 服务最多并行执行 3 个视频任务。
- 最多允许 32 个 `queued` 或 `running` 任务。
- 终态任务的状态和结果只保存在内存中，完成后保留 1 小时，最多保留 100 条。
- 重启或关闭 BiliTranscript 后，任务不会恢复。
- 登录浏览器和上游 ASR 等受限资源仍可能在多个任务之间串行使用，因此三个任务不一定同时进入相同的提取步骤。

提交、状态查询和取消都是短请求，调用方通常设置 30 秒 HTTP 超时即可。文稿提取在后台任务中继续执行，不需要让提交请求保持连接。使用 ASR 时，单个任务可能运行很久；BiliTranscript 对上游 OpenAI 兼容 ASR 的默认转录超时为 3600 秒，但这与提取 API 客户端的单次 HTTP 超时不是一回事。

## 10. 提取 API 与 ASR API 的区别

这两个端口用途不同：

| 默认端口 | 服务 | 调用方向 | 用途 |
| --- | --- | --- | --- |
| `8766` | BiliTranscript 提取 API | 其他软件调用 BiliTranscript | 输入 B站视频，取得完整文稿 |
| `8765` | CrisperWeaver / MiMo 等上游 ASR API | BiliTranscript 调用 ASR 软件 | 输入音频，取得语音识别结果 |

其他软件需要提取 B站文稿时，应调用 `8766`。只有在为 BiliTranscript 配置外部语音识别后端时，才需要关心 `8765`。

## 11. 常见问题

### 健康检查正常，但 `/v1` 返回 401

确认请求头是完整的 `Authorization: Bearer <API_KEY>`，并使用当前设置窗口中的 Key。重新生成 Key 后，旧 Key 立即失效。

### 本机可以访问，局域网其他设备无法访问

确认调用的是设置窗口显示的 `192.168.x.x` 或其他局域网 IPv4 地址，而不是 `127.0.0.1` 或 `0.0.0.0`。同时检查两台设备是否在同一网络，并在 Windows 防火墙中允许 `BiliTranscript.exe` 的专用网络访问。

### 结果接口返回 409

任务尚未成功完成。先调用状态接口；`queued` 或 `running` 时继续等待，`failed` 时查看状态响应的 `error`，`cancelled` 时需要重新提交。

### 状态接口返回 404

任务 ID 可能错误，任务也可能已完成超过 1 小时，或者 BiliTranscript 在任务创建后已经重启。此时无法从服务恢复原结果。

### 如何批量调用

每个 `POST /v1/extractions` 只接受一个视频。调用方可以拆分视频列表并分别提交任务，但应遵守最多 32 个未结束任务的限制；遇到 `429` 时等待后再提交。
