# image-gen-mcp

[中文](README.md)

A **Model Context Protocol (MCP)** server that exposes image generation and editing as async tools for Claude (Cowork / Desktop / Code) or any other MCP host.

Calls are non-blocking: `generate_image` and `edit_image` return a `task_id` immediately (well within any 60-second MCP timeout). The host then polls `check_task` until the result is ready — even if the upstream takes 3+ minutes to generate.

Supports any OpenAI-compatible image API (`gpt-image-2`, DALL·E-style backends, or chat-completions wrappers).

---

## Sponsors

Compute for this project is sponsored by **[Codox](https://www.codox.cc)** — an API platform supporting GPT (Codex), Claude Sonnet/Opus, and more. Compatible with Codex CLI, Claude Code, and Claude Desktop. **1 CNY = $5 USD in credits.** Give it a try!

---

## Tools

| Tool | Description |
|------|-------------|
| `generate_image` | Submit a text-to-image task. Returns `task_id` immediately. |
| `edit_image` | Submit an image-edit task. Supports inpainting, maskless editing, and up to 16 reference images. Returns `task_id` immediately. |
| `check_task` | Poll a task by `task_id`. Returns `status` and `saved_paths` when done. |
| `list_task_history` | List the 50 most recent tasks (filterable by status). |
| `list_generated_images` | List files in a directory by glob. |

### Async flow

```
generate_image(prompt="...")
  → { task_id: "abc123", status: "pending" }

check_task(task_id="abc123")   ← poll every 10-30 s
  → { status: "running" }

check_task(task_id="abc123")
  → { status: "done", saved_paths: ["C:/Users/User/Claude/img_xxx.png"], elapsed_seconds: 87.3 }
```

---

## Install

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

## Register the server

### Claude Cowork / Desktop (Windows)

Config file: `C:\Users\<you>\AppData\Local\Claude-3p\claude_desktop_config.json`

```json
"image-gen": {
  "command": "C:\\Users\\<you>\\.claude\\image_gen_mcp\\.venv\\Scripts\\python.exe",
  "args": ["C:\\Users\\<you>\\.claude\\image_gen_mcp\\server.py"],
  "env": {
    "IMAGE_API_BASE_URL": "https://your-api-host",
    "IMAGE_API_KEY": "sk-..."
  }
}
```

### Claude Cowork / Desktop (macOS / Linux)

```json
"image-gen": {
  "command": "/home/<you>/.local/share/image-gen-mcp/.venv/bin/python",
  "args": ["/home/<you>/.local/share/image-gen-mcp/server.py"],
  "env": {
    "IMAGE_API_BASE_URL": "https://your-api-host",
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
        "IMAGE_API_BASE_URL": "https://your-api-host",
        "IMAGE_API_KEY": "sk-..."
      }
    }
  }
}
```

Restart the host after editing. You should see `image-gen` in MCP plugins with 5 tools.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IMAGE_API_BASE_URL` | *(required)* | Upstream root URL, e.g. `https://www.codox.cc`. No trailing slash — the server appends `/v1/images/generations` etc. |
| `IMAGE_API_KEY` | *(required)* | Bearer token. |
| `IMAGE_API_BACKEND` | `images` | `images` / `responses` / `chat`. See [Backends](#backends). |
| `IMAGE_DEFAULT_MODEL` | `gpt-image-2` | Default model if not passed by the caller. |
| `IMAGE_MAX_CONCURRENT` | `10` | Max concurrent in-flight requests (asyncio semaphore). |
| `IMAGE_REQUEST_TIMEOUT` | `1800` | Per-request timeout in seconds. |
| `IMAGE_RETRY_ATTEMPTS` | `3` | Retry count on transient errors. Backoff: 2 s / 5 s / 10 s. |
| `IMAGE_GEN_PATH` | `/v1/images/generations` | Override generation endpoint path. |
| `IMAGE_EDIT_PATH` | `/v1/images/edits` | Override edit endpoint path. |
| `IMAGE_CHAT_PATH` | `/v1/chat/completions` | Override chat backend path. |
| `IMAGE_RESPONSES_PATH` | `/v1/responses` | Override responses backend path. |

---

## Backends

| Backend | Endpoint | Notes |
|---------|----------|-------|
| `images` *(default)* | `POST /v1/images/generations` | Standard OpenAI image API. Prefer this. |
| `responses` | `POST /v1/responses` | Newer Responses API with tool-call style. |
| `chat` | `POST /v1/chat/completions` | For providers that wrap image generation in chat completions. Size/quality constraints are text-encoded into the prompt. |

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
  "extra": {},
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

Native gpt-image-2 editing modes:

- **Inpainting** — provide `mask_path` (PNG with alpha); transparent pixels = edit region, opaque = preserved
- **Maskless editing** — omit `mask_path`; describe the change in `prompt`, model locates the region automatically
- **Multi-image composition / style transfer** — pass up to 16 paths via `image_paths`; first is primary, rest are references

```jsonc
{
  "prompt": "Replace the background with a sunset beach, keep the subject",
  "image_path": "C:/Users/User/Claude/photo.png",   // single image OR use image_paths
  "image_paths": [                                   // up to 16 images
    "C:/Users/User/Claude/primary.png",
    "C:/Users/User/Claude/style_ref.png"
  ],
  "mask_path": "C:/Users/User/Claude/mask.png",     // optional; transparent = edit area
  "size": "auto",            // auto | 1024x1024 | 1536x1024 | 1024x1536
  "n": 1,                    // 1-10 output variants
  "output_compression": 85,  // 0-100, jpeg/webp only
  "output_dir": "C:/Users/User/Claude",
  "filename_prefix": "edited"
}
```

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
  "elapsed_seconds": 87.3
}
```

---

## Tips

1. Always set `output_dir` to an absolute path you have write access to.
2. Set `filename_prefix` so outputs are easy to identify.
3. Portraits: `1024x1536`; landscapes: `1536x1024`; square: `1024x1024`.
4. Write prompts in the language of the text you want rendered in the image.
5. `quality: "high"` produces better results but takes 2-3× longer.
6. Multiple tasks can be submitted simultaneously — the worker runs up to `IMAGE_MAX_CONCURRENT` (default 10) in parallel.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Plugin shows as disconnected | Wrong path in `command` / `args` | Verify both are absolute paths and `.venv` exists |
| `IMAGE_API_BASE_URL env var is not set` | Missing `env` block in config | Add the `env` block as a sibling of `args` |
| `Upstream error 401/403` | Bad API key | Check `IMAGE_API_KEY` |
| `Upstream error 404` | Wrong backend or endpoint | Try a different backend; override endpoint path env vars |
| Task stuck at `pending` | Worker not started | Restart the MCP host |

---

## Security

- The API key is never logged and never written to disk.
- `output_dir` is resolved to an absolute path; relative paths are rejected.
- No image content is read back to the upstream API.

---

## License

MIT
