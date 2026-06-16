# image-gen-mcp

[English](README.en.md)

一个基于 **Model Context Protocol (MCP)** 的图片生成与编辑服务，可接入 Claude Cowork / Desktop / Code 或任意支持 MCP 的客户端。

调用是**非阻塞**的：`generate_image` 和 `edit_image` 立即返回 `task_id`（远在 60 秒超时之前），客户端随后通过 `check_task` 轮询结果——即使上游生图需要 3 分钟也没问题。

兼容任何 OpenAI 格式的图片 API（`gpt-image-2`、DALL·E 系列、chat-completions 封装等）。

---

## 赞助

本项目使用的算力由 **[Codox](https://www.codox.cc)** 赞助提供。

Codox 是一个提供主流模型算力的平台，支持 GPT (Codex)、Claude Sonnet / Opus 等常用模型，可直接接入 Codex CLI、Claude Code、Claude Desktop 等工具使用。**优惠汇率：1 元人民币 = 5 美元额度**，欢迎大家前往体验。

---

## 工具列表

| 工具 | 说明 |
|------|------|
| `generate_image` | 提交文生图任务，立即返回 `task_id` |
| `edit_image` | 提交图片编辑任务，支持蒙版修图、无蒙版编辑、最多 16 张参考图，立即返回 `task_id` |
| `check_task` | 通过 `task_id` 查询任务状态，完成后返回文件路径 |
| `list_task_history` | 查看最近 50 条任务历史，可按状态筛选 |
| `list_generated_images` | 按 glob 列出目录中的图片文件 |

### 异步流程

```
generate_image(prompt="...")
  → { task_id: "abc123", status: "pending" }

check_task(task_id="abc123")   ← 每隔 10-30 秒轮询
  → { status: "running" }

check_task(task_id="abc123")
  → { status: "done", saved_paths: ["C:/Users/User/Claude/img_xxx.png"], elapsed_seconds: 87.3 }
```

---

## 安装

### 前提条件

- Python 3.10+

### Windows

```powershell
git clone https://github.com/xcdd/image-gen-mcp.git "$env:USERPROFILE\.claude\image_gen_mcp"
cd "$env:USERPROFILE\.claude\image_gen_mcp"
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### Linux / macOS

```bash
git clone https://github.com/xcdd/image-gen-mcp.git ~/.local/share/image-gen-mcp
cd ~/.local/share/image-gen-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

---

## 注册到 MCP 客户端

### Claude Cowork / Desktop（Windows）

配置文件：`C:\Users\<用户名>\AppData\Local\Claude-3p\claude_desktop_config.json`

```json
"image-gen": {
  "command": "C:\\Users\\<用户名>\\.claude\\image_gen_mcp\\.venv\\Scripts\\python.exe",
  "args": ["C:\\Users\\<用户名>\\.claude\\image_gen_mcp\\server.py"],
  "env": {
    "IMAGE_API_BASE_URL": "https://your-api-host",
    "IMAGE_API_KEY": "sk-..."
  }
}
```

### Claude Cowork / Desktop（macOS / Linux）

配置文件：`~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）

```json
"image-gen": {
  "command": "/home/<用户名>/.local/share/image-gen-mcp/.venv/bin/python",
  "args": ["/home/<用户名>/.local/share/image-gen-mcp/server.py"],
  "env": {
    "IMAGE_API_BASE_URL": "https://your-api-host",
    "IMAGE_API_KEY": "sk-..."
  }
}
```

### Claude Code

编辑 `~/.claude/mcp.json`：

```json
{
  "mcpServers": {
    "image-gen": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"],
      "env": {
        "IMAGE_API_BASE_URL": "https://your-api-host",
        "IMAGE_API_KEY": "sk-..."
      }
    }
  }
}
```

配置完成后重启客户端，MCP 插件列表中应出现 `image-gen`，包含 5 个工具。

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `IMAGE_API_BASE_URL` | **必填** | 上游 API 根地址，例如 `https://www.codox.cc`。不含尾部斜杠，服务端会自动拼接 `/v1/images/generations` 等路径 |
| `IMAGE_API_KEY` | **必填** | Bearer Token |
| `IMAGE_API_BACKEND` | `images` | 后端类型：`images` / `responses` / `chat`，见[后端说明](#后端说明) |
| `IMAGE_DEFAULT_MODEL` | `gpt-image-2` | 调用方未指定 model 时的默认值 |
| `IMAGE_MAX_CONCURRENT` | `10` | 最大并发请求数（asyncio 信号量） |
| `IMAGE_REQUEST_TIMEOUT` | `1800` | 单次请求超时秒数，高质量生图可能需要 3 分钟以上 |
| `IMAGE_RETRY_ATTEMPTS` | `3` | 遇到 5xx / 超时 / 连接重置时的重试次数，退避间隔 2s / 5s / 10s |
| `IMAGE_GEN_PATH` | `/v1/images/generations` | 生图端点路径（可覆盖） |
| `IMAGE_EDIT_PATH` | `/v1/images/edits` | 编辑端点路径（可覆盖） |
| `IMAGE_CHAT_PATH` | `/v1/chat/completions` | chat 后端端点路径 |
| `IMAGE_RESPONSES_PATH` | `/v1/responses` | responses 后端端点路径 |

---

## 后端说明

| 后端 | 端点 | 适用场景 |
|------|------|---------|
| `images`（默认） | `POST /v1/images/generations` | 标准 OpenAI 图片 API，优先选择 |
| `responses` | `POST /v1/responses` | 新版 Responses API（工具调用形式） |
| `chat` | `POST /v1/chat/completions` | 部分平台通过 chat completions 封装图片生成，尺寸/质量参数以文本形式注入 prompt |

---

## 工具参数详解

### `generate_image`

```jsonc
{
  "prompt": "水彩风格的猫咪坐在窗台上",  // 必填
  "model": "gpt-image-2",
  "size": "1024x1024",       // 1024x1024 | 1536x1024 | 1024x1536 | 2048x2048
  "n": 1,                    // 1-4
  "quality": "auto",         // auto | high | medium | low
  "output_format": "png",    // png | jpeg | webp
  "negative_prompt": "模糊, 低质量",
  "style_intensity": 50,     // 风格强度 0-100
  "background": "auto",      // auto | transparent | white | opaque
  "seed": 42,
  "extra": {},               // 原样透传给 API 的额外字段
  "output_dir": "C:/Users/User/Claude",
  "filename_prefix": "cat_watercolor",
  "backend": "images"
}
```

立即返回：

```json
{ "task_id": "3f9a...", "status": "pending", "message": "Task submitted. Use check_task to poll for completion." }
```

---

### `edit_image`

支持 gpt-image-2 全部原生编辑模式：

- **蒙版修图（Inpainting）**：提供 `mask_path`（含 Alpha 通道的 PNG），透明区域为待编辑区域，不透明区域保持不变
- **无蒙版编辑**：不提供 `mask_path`，通过 `prompt` 描述要修改的内容，模型自动定位区域
- **多图合成 / 风格迁移**：通过 `image_paths` 提供最多 16 张图，第一张为主图，其余作为参考

```jsonc
{
  "prompt": "把背景换成日落海滩，保留主体人物",  // 必填
  "image_path": "C:/Users/User/Claude/photo.png",    // 单张图（与 image_paths 二选一）
  "image_paths": [                                    // 多张图（最多 16 张）
    "C:/Users/User/Claude/primary.png",
    "C:/Users/User/Claude/style_ref.png"
  ],
  "mask_path": "C:/Users/User/Claude/mask.png",      // 可选，透明区域 = 编辑范围
  "model": "gpt-image-2",
  "size": "auto",            // auto | 1024x1024 | 1536x1024 | 1024x1536
  "n": 1,                    // 输出变体数量 1-10
  "quality": "auto",
  "output_format": "png",
  "output_compression": 85,  // 压缩率 0-100，仅对 jpeg/webp 生效
  "output_dir": "C:/Users/User/Claude",
  "filename_prefix": "edited",
  "backend": "images"
}
```

同样立即返回 `task_id`，用 `check_task` 轮询。

---

### `check_task`

```jsonc
{ "task_id": "3f9a..." }
```

完成时的返回示例：

```json
{
  "task_id": "3f9a...",
  "status": "done",
  "saved_paths": ["C:/Users/User/Claude/cat_watercolor.png"],
  "created_at": 1750123456.0,
  "finished_at": 1750123543.3,
  "elapsed_seconds": 87.3
}
```

---

### `list_task_history`

```jsonc
{ "status_filter": "done" }   // all | done | error | pending | running
```

返回最近 50 条任务，按完成时间倒序排列。

---

### `list_generated_images`

```jsonc
{ "directory": "C:/Users/User/Claude", "pattern": "img_*" }
```

---

## 使用建议

1. **始终指定 `output_dir`** 为绝对路径，默认值仅适用于作者本机
2. **设置 `filename_prefix`** 让输出文件名有意义，便于后续管理
3. 竖版海报用 `1024x1536`，横版用 `1536x1024`，方形用 `1024x1024`
4. 图片内需要出现文字时，prompt 使用对应语言效果更佳
5. `quality: "high"` 效果更好，但耗时约为默认的 2-3 倍
6. 可同时提交多个任务，worker 默认并发 10 个（由 `IMAGE_MAX_CONCURRENT` 控制）

---

## 常见问题

| 现象 | 可能原因 | 解决方法 |
|------|---------|---------|
| MCP 插件显示断开 | `command` 或 `args` 路径错误 | 确认两者均为绝对路径，且 `.venv` 已创建 |
| `IMAGE_API_BASE_URL env var is not set` | 配置 JSON 缺少 `env` 块 | 在与 `args` 同级位置添加 `env` 块 |
| `Upstream error 401/403` | API Key 无效 | 检查 `IMAGE_API_KEY` |
| `Upstream error 404` | 后端类型或端点路径错误 | 换用其他 `IMAGE_API_BACKEND`，或覆盖端点路径变量 |
| 任务一直 pending | Worker 未启动（异常） | 重启 MCP 客户端 |

---

## 安全说明

- API Key 不会被记录日志，也不会写入磁盘，仅存在于进程环境变量中
- `output_dir` 会被解析为绝对路径并自动创建，相对路径会被拒绝
- 输出目录中的图片内容不会被回传给上游 API

---

## License

MIT
