# image-gen-mcp

A **Model Context Protocol (MCP)** server that exposes image generation and editing as async tools for Claude (Cowork / Desktop / Code) or any other MCP host.

Calls are non-blocking: `generate_image` and `edit_image` return a `task_id` immediately (well within any 60-second MCP timeout). The host then polls `check_task` until the result is ready — even if the upstream takes 3+ minutes to generate.

Supports any OpenAI-compatible image API (`gpt-image-2`, DALL·E-style backends, or chat-completions wrappers).

---

## Tools

| Tool | Description |
|------|-------------|
| `generate_image` | Submit a text-to-image task. Returns `task_id` immediately. |
| `edit_image` | Submit an image-edit task. Supports inpainting, maskless editing, and up to 16 reference images. Returns `task_id` immediately. |
| `check_task` | Poll a task by `task_id`. Returns `status` and `saved_paths` when done. |
| `list_task_history` | List the 50 most recent tasks (filterable by status). |
| `list_generated_images` | List files in a directory by glob (handy for enumerating prior outputs). |

### Async flow

```
generate_image(prompt="...")
  → { task_id: "abc123", status: "pending" }

check_task(task_id="abc123")   ← poll every 10-30 s
  → { status: "running" }

check_task(task_id="abc123")
  → { status: "done", saved_paths: ["C:/Users/User/Claude/img_20250617_120501.png"], elapsed_seconds: 87.3 }
```

---

## Install

### Prerequisites

- Python 3.10+
- `pip` or `uv`

### Linux / macOS

```bash
git clone https://github.com/xcdd/image-gen-mcp.git ~/.local/share/image-gen-mcp
cd ~/.local/share/image-gen-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Windows

```powershell
git clone https://github.com/xcdd/image-gen-mcp.git "$env:USERPROFILE\.claude\image_gen_mcp"
cd "$env:USERPROFILE\.claude\image_gen_mcp"
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

---

## Register the server

Add an entry to the MCP host's config under `mcpServers`.

### Claude Cowork / Desktop (Windows)

Config file: `C:\Users\<you>\AppData\Local\Claude-3p\claude_desktop_config.json`

```json
"image-gen": {
  "command": "C:\\Users\\<you>\\.claude\\image_gen_mcp\\.venv\\Scripts\\python.exe",
  "args": ["C:\\Users\\<you>\\.claude\\image_gen_mcp\\server.py"],
  "env": {
    "IMAGE_API_BASE_URL": "https://your-openai-compatible-host",
    "IMAGE_API_KEY": "sk-..."
  }
}
```

### Claude Cowork / Desktop (macOS / Linux)

Config file: `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `~/.config/Claude/claude_desktop_config.json` (Linux)

```json
"image-gen": {
  "command": "/home/<you>/.local/share/image-gen-mcp/.venv/bin/python",
  "args": ["/home/<you>/.local/share/image-gen-mcp/server.py"],
  "env": {
    "IMAGE_API_BASE_URL": "https://your-openai-compatible-host",
    "IMAGE_API_KEY": "sk-..."
  }
}
```

### Claude Code

Edit `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "image-gen": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/server.py"],
      "env": {
        "IMAGE_API_BASE_URL": "https://your-openai-compatible-host",
        "IMAGE_API_KEY": "sk-..."
      }
    }
  }
}
```

Restart the host after editing. You should see `image-gen` listed in MCP plugins with 5 tools.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_API_BASE_URL` | *(required)* | Upstream root URL, e.g. `https://api.openai.com`. No trailing slash. The server appends `/v1/images/generations` etc. |
| `IMAGE_API_KEY` | *(required)* | Bearer token (`Authorization: Bearer ...`). |
| `IMAGE_API_BACKEND` | `images` | `images` / `responses` / `chat`. See [Backends](#backends). |
| `IMAGE_DEFAULT_MODEL` | `gpt-image-2` | Default model if not passed by the caller. |
| `IMAGE_MAX_CONCURRENT` | `10` | Max concurrent in-flight requests (asyncio semaphore). |
| `IMAGE_REQUEST_TIMEOUT` | `1800` | Per-request timeout in seconds. High-quality generations can take 3+ minutes. |
| `IMAGE_RETRY_ATTEMPTS` | `3` | Retry count on transient errors (5xx, timeout, connection reset). Backoff: 2 s / 5 s / 10 s. |
| `IMAGE_GEN_PATH` | `/v1/images/generations` | Override endpoint path for generation. |
| `IMAGE_EDIT_PATH` | `/v1/images/edits` | Override endpoint path for edits. |
| `IMAGE_CHAT_PATH` | `/v1/chat/completions` | Override endpoint path for chat backend. |
| `IMAGE_RESPONSES_PATH` | `/v1/responses` | Override endpoint path for responses backend. |

---

## Backends

| Backend | Endpoint | Notes |
|---------|----------|-------|
| `images` *(default)* | `POST /v1/images/generations` | Standard OpenAI image API. Use this unless the provider says otherwise. |
| `responses` | `POST /v1/responses` | Newer Responses API with tool-call style. |
| `chat` | `POST /v1/chat/completions` | For providers that wrap image generation inside chat completions. Size / quality constraints are encoded into the prompt as text hints. |

---

## Tool reference

### `generate_image`

```jsonc
{
  "prompt": "A watercolor cat on a sunny windowsill",  // required
  "model": "gpt-image-2",
  "size": "1024x1024",       // 1024x1024 | 1536x1024 | 1024x1536 | 2048x2048
  "n": 1,                    // 1-4
  "quality": "auto",         // auto | high | medium | low
  "output_format": "png",    // png | jpeg | webp
  "negative_prompt": "blurry, low quality",
  "style_intensity": 50,     // 0-100
  "background": "auto",      // auto | transparent | white | opaque
  "seed": 42,
  "extra": {},               // raw fields passed through to the API
  "output_dir": "C:/Users/User/Claude",
  "filename_prefix": "cat_watercolor",
  "backend": "images"
}
```

Returns immediately:

```json
{ "task_id": "3f9a...", "status": "pending", "message": "Task submitted. Use check_task to poll for completion." }
```

---

### `edit_image`

Supports all native gpt-image-2 editing modes:

- **Inpainting** — provide `mask_path` (PNG with alpha channel); transparent pixels define the region to edit, opaque pixels are preserved.
- **Maskless editing** — omit `mask_path`; describe what to change in `prompt` and the model locates the region automatically.
- **Multi-image composition / style transfer** — pass up to 16 source images via `image_paths`; the first is the primary image, the rest serve as references.

```jsonc
{
  "prompt": "Replace the background with a sunset beach, keep the subject",  // required
  "image_path": "C:/Users/User/Claude/photo.png",   // single image (OR use image_paths)
  "image_paths": [                                   // up to 16 images
    "C:/Users/User/Claude/primary.png",
    "C:/Users/User/Claude/style_ref.png"
  ],
  "mask_path": "C:/Users/User/Claude/mask.png",     // optional; transparent = edit area
  "model": "gpt-image-2",
  "size": "auto",      // auto | 1024x1024 | 1536x1024 | 1024x1536
  "n": 1,              // 1-10 output variants
  "quality": "auto",
  "output_format": "png",
  "output_compression": 85,   // 0-100, only for jpeg/webp
  "output_dir": "C:/Users/User/Claude",
  "filename_prefix": "edited",
  "backend": "images"
}
```

Returns the same `task_id` pattern as `generate_image`.

---

### `check_task`

```jsonc
{ "task_id": "3f9a..." }
```

Response when done:

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

Returns the 50 most recent completed tasks (most recent first).

---

### `list_generated_images`

```jsonc
{ "directory": "C:/Users/User/Claude", "pattern": "img_*" }
```

---

## Tips for callers

1. **Always set `output_dir`** to an absolute path the user has write access to. The default `C:/Users/User/Claude` is only correct on the author's machine.
2. **Set `filename_prefix`** so outputs are easy to identify later.
3. For portraits / posters prefer `1024x1536`; for landscapes `1536x1024`.
4. For text in the image, write the prompt in the same language as the desired text — gpt-image-2 handles Chinese and English well.
5. `quality: "high"` produces better results but may take 2-3× longer.
6. Multiple calls to `generate_image` can be submitted without waiting — the worker runs up to `IMAGE_MAX_CONCURRENT` (default 10) in parallel.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| MCP plugin shows as disconnected | Wrong path in `command` / `args` | Verify both are absolute paths and `.venv` exists |
| `IMAGE_API_BASE_URL env var is not set` | Missing `env` block in config | Add the `env` block as a sibling of `args` in the config JSON |
| `Upstream error 401/403` | Bad API key | Check `IMAGE_API_KEY` |
| `Upstream error 404` | Wrong backend or endpoint | Try a different `IMAGE_API_BACKEND`; override `IMAGE_EDIT_PATH` if needed |
| Task stays `pending` forever | Worker not started (bug) | Restart the MCP host to reload the server |
| Image looks wrong but no error | Prompt / model limitation | Iterate on prompt; try `quality: "high"` |

---

## Security

- The API key is never logged and never written to disk. It only lives in the process environment.
- `output_dir` is resolved to an absolute path and created if absent. Relative paths are rejected.
- No image content is read back to the upstream.

---

## License

MIT
