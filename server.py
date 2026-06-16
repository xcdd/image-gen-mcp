#!/usr/bin/env python3
"""Image Generation MCP Server — async task queue edition."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------
API_BASE_URL = os.environ.get("IMAGE_API_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("IMAGE_API_KEY", "")

BACKEND = os.environ.get("IMAGE_API_BACKEND", "images").lower()

IMAGES_GEN_PATH = os.environ.get("IMAGE_GEN_PATH", "/v1/images/generations")
IMAGES_EDIT_PATH = os.environ.get("IMAGE_EDIT_PATH", "/v1/images/edits")
RESPONSES_PATH = os.environ.get("IMAGE_RESPONSES_PATH", "/v1/responses")
CHAT_PATH = os.environ.get("IMAGE_CHAT_PATH", "/v1/chat/completions")

DEFAULT_MODEL = os.environ.get("IMAGE_DEFAULT_MODEL", "gpt-image-2")
_MAX_CONCURRENT = int(os.environ.get("IMAGE_MAX_CONCURRENT", "10"))

ALLOWED_SIZES = {"1024x1024", "1536x1024", "1024x1536", "2048x2048"}
ALLOWED_EDIT_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
ALLOWED_QUALITIES = {"auto", "high", "medium", "low"}
ALLOWED_FORMATS = {"png", "jpeg", "webp"}
ALLOWED_BACKGROUNDS = {"auto", "transparent", "white", "opaque"}

app = Server("image-gen")

# ---------------------------------------------------------------------------
# Async task queue
# ---------------------------------------------------------------------------
# task: { id, status, created_at, finished_at, args, result, error }
_task_store: dict[str, dict] = {}
_task_history: deque = deque(maxlen=50)
_worker_task: asyncio.Task | None = None
_semaphore: asyncio.Semaphore | None = None


def _ensure_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


async def _worker() -> None:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    while True:
        pending = [t for t in _task_store.values() if t["status"] == "pending"]
        for task in pending:
            task["status"] = "running"
            asyncio.create_task(_run_one(task))
        await asyncio.sleep(1)


async def _run_one(task: dict) -> None:
    async with _semaphore:
        try:
            if task.get("type") == "edit":
                saved = await asyncio.to_thread(_run_edit_sync, task["args"])
            else:
                saved = await asyncio.to_thread(_run_generate_sync, task["args"])
            task["status"] = "done"
            task["result"] = saved
        except Exception as e:
            task["status"] = "error"
            task["error"] = str(e)
        task["finished_at"] = time.time()
        _task_history.append(task)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_output_dir(output_dir: str) -> Path:
    p = Path(output_dir).resolve()
    if not p.is_absolute():
        raise ValueError(f"output_dir must be an absolute path, got: {output_dir}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _timestamp_prefix() -> str:
    return datetime.now().strftime("img_%Y%m%d_%H%M%S")


def _ext_for_format(fmt: str) -> str:
    return "jpg" if fmt == "jpeg" else fmt


def _save_b64(b64: str, path: Path) -> Path:
    path.write_bytes(base64.b64decode(b64))
    return path


def _save_url(url: str, path: Path) -> Path:
    resp = httpx.get(url, timeout=120)
    resp.raise_for_status()
    path.write_bytes(resp.content)
    return path


def _build_prompt_with_hints(prompt: str, *, size: str, n: int, quality: str,
                              negative_prompt: str | None, style_intensity: int,
                              background: str, seed: int | None) -> str:
    hints = []
    if size:
        hints.append(f"size {size}")
    if n and n > 1:
        hints.append(f"return {n} images")
    if quality and quality != "auto":
        hints.append(f"quality {quality}")
    if background and background != "auto":
        hints.append(f"background {background}")
    if style_intensity is not None and style_intensity != 50:
        hints.append(f"style intensity {style_intensity}")
    if seed is not None:
        hints.append(f"seed {seed}")
    if negative_prompt:
        hints.append(f"avoid: {negative_prompt}")
    if hints:
        return f"{prompt}\n\n[Constraints: {', '.join(hints)}]"
    return prompt


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


_RETRY_ATTEMPTS = int(os.environ.get("IMAGE_RETRY_ATTEMPTS", "3"))
_RETRY_BACKOFF = (2.0, 5.0, 10.0)
_REQUEST_TIMEOUT = float(os.environ.get("IMAGE_REQUEST_TIMEOUT", "1800"))


def _post_with_retry(endpoint: str, *, json_body: dict | None = None,
                     data: dict | None = None, files: dict | None = None,
                     headers: dict | None = None) -> "httpx.Response":
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            if files is not None:
                with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
                    resp = client.post(endpoint, data=data, files=files, headers=headers)
            else:
                resp = httpx.post(endpoint, json=json_body, headers=headers,
                                  timeout=_REQUEST_TIMEOUT)
            if resp.status_code >= 500 and attempt < _RETRY_ATTEMPTS - 1:
                last_exc = RuntimeError(f"upstream {resp.status_code}: {resp.text[:200]}")
                time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
                continue
            return resp
        except (httpx.RemoteProtocolError, httpx.ConnectError,
                httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)])
                continue
            break
    raise RuntimeError(f"Request failed after {_RETRY_ATTEMPTS} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Backend: images
# ---------------------------------------------------------------------------

def _call_images_generate(model: str, prompt: str, *, size: str, n: int, quality: str,
                          fmt: str, background: str, negative_prompt: str | None,
                          seed: int | None, extra: dict) -> list[dict]:
    if not API_BASE_URL:
        raise RuntimeError("IMAGE_API_BASE_URL env var is not set")
    endpoint = f"{API_BASE_URL}{IMAGES_GEN_PATH}"
    payload: dict = {
        "model": model, "prompt": prompt, "size": size, "n": n,
        "quality": quality, "output_format": fmt,
    }
    if background != "auto":
        payload["background"] = background
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if seed is not None:
        payload["seed"] = seed
    payload.update(extra)
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    resp = _post_with_retry(endpoint, json_body=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Upstream error {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    items: list[dict] = []
    for d in body.get("data", []):
        if "b64_json" in d:
            items.append({"b64_json": d["b64_json"]})
        elif "url" in d:
            items.append({"url": d["url"]})
    if not items:
        raise RuntimeError(f"No image data returned: {json.dumps(body)[:300]}")
    return items


def _call_images_edit(model: str, prompt: str, image_paths: list[str],
                      mask_path: str | None, *, size: str, n: int, quality: str,
                      fmt: str, output_compression: int | None) -> list[dict]:
    """Call /v1/images/edits (multipart).

    image_paths: 1-16 source images. First is the primary; extras are references.
    mask_path: optional PNG with alpha channel — transparent pixels = edit region.
               Omit to let the model infer the edit region from the prompt.
    output_compression: 0-100, only for jpeg/webp.
    """
    if not API_BASE_URL:
        raise RuntimeError("IMAGE_API_BASE_URL env var is not set")
    endpoint = f"{API_BASE_URL}{IMAGES_EDIT_PATH}"
    form: dict = {
        "model": model, "prompt": prompt, "n": str(n),
        "quality": quality, "output_format": fmt,
    }
    if size != "auto":
        form["size"] = size
    if output_compression is not None and fmt in ("jpeg", "webp"):
        form["output_compression"] = str(output_compression)
    # Multiple images supported (up to 16); httpx multipart accepts list of tuples
    files: list[tuple] = []
    for ip in image_paths[:16]:
        files.append(("image", (Path(ip).name, open(ip, "rb").read())))
    if mask_path:
        files.append(("mask", (Path(mask_path).name, open(mask_path, "rb").read())))
    with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
        resp = client.post(endpoint, data=form, files=files, headers=_auth_headers())
    if resp.status_code != 200:
        raise RuntimeError(f"Upstream error {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    items: list[dict] = []
    for d in body.get("data", []):
        if "b64_json" in d:
            items.append({"b64_json": d["b64_json"]})
        elif "url" in d:
            items.append({"url": d["url"]})
    if not items:
        raise RuntimeError(f"No image data returned: {json.dumps(body)[:300]}")
    return items


# ---------------------------------------------------------------------------
# Backend: responses
# ---------------------------------------------------------------------------

def _call_responses_generate(model: str, prompt: str, *, size: str, n: int, quality: str,
                              fmt: str, background: str, negative_prompt: str | None,
                              seed: int | None, extra: dict) -> list[dict]:
    if not API_BASE_URL:
        raise RuntimeError("IMAGE_API_BASE_URL env var is not set")
    endpoint = f"{API_BASE_URL}{RESPONSES_PATH}"
    tool: dict = {"type": "image_generation", "size": size, "output_format": fmt, "quality": quality}
    if background != "auto":
        tool["background"] = background
    full_input = prompt
    if negative_prompt:
        full_input += f"\n\nAvoid: {negative_prompt}"
    if seed is not None:
        full_input += f"\n[seed={seed}]"
    payload: dict = {"model": model, "input": full_input, "tools": [tool]}
    payload.update(extra)
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    items: list[dict] = []
    for _ in range(max(1, n)):
        resp = _post_with_retry(endpoint, json_body=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Upstream error {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        for out in body.get("output", []):
            if out.get("type") == "image_generation_call":
                result_b64 = out.get("result")
                if result_b64:
                    items.append({"b64_json": result_b64})
    if not items:
        raise RuntimeError("No image_generation_call output found")
    return items


# ---------------------------------------------------------------------------
# Backend: chat
# ---------------------------------------------------------------------------

def _call_chat_generate(model: str, prompt: str, n: int) -> list[dict]:
    if not API_BASE_URL:
        raise RuntimeError("IMAGE_API_BASE_URL env var is not set")
    endpoint = f"{API_BASE_URL}{CHAT_PATH}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    items: list[dict] = []
    for _ in range(max(1, n)):
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False}
        resp = _post_with_retry(endpoint, json_body=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Upstream error {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        try:
            content_list = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected response shape: {json.dumps(body)[:500]}") from exc
        parts = content_list if isinstance(content_list, list) else [{"type": "text", "text": content_list}]
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    items.append({"b64_json": url.split(";base64,", 1)[1]})
                elif url:
                    items.append({"url": url})
    if not items:
        raise RuntimeError("No images returned by upstream")
    return items


def _call_chat_edit(model: str, prompt: str, image_path: str, mask_path: str | None) -> list[dict]:
    if not API_BASE_URL:
        raise RuntimeError("IMAGE_API_BASE_URL env var is not set")
    endpoint = f"{API_BASE_URL}{CHAT_PATH}"
    headers = {**_auth_headers(), "Content-Type": "application/json"}

    def _to_data_url(p: str) -> str:
        data = Path(p).read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        ext = Path(p).suffix.lstrip(".").lower() or "png"
        mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
        return f"data:{mime};base64,{b64}"

    content_parts: list[dict] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _to_data_url(image_path)}},
    ]
    if mask_path:
        content_parts.append({"type": "image_url", "image_url": {"url": _to_data_url(mask_path)}})
    payload = {"model": model, "messages": [{"role": "user", "content": content_parts}], "stream": False}
    resp = _post_with_retry(endpoint, json_body=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Upstream error {resp.status_code}: {resp.text[:500]}")
    body = resp.json()
    try:
        content_list = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected response shape: {json.dumps(body)[:500]}") from exc
    items: list[dict] = []
    parts = content_list if isinstance(content_list, list) else [{"type": "text", "text": content_list}]
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                items.append({"b64_json": url.split(";base64,", 1)[1]})
            elif url:
                items.append({"url": url})
    if not items:
        raise RuntimeError("No images returned by upstream")
    return items


def _save_results(items: list[dict], output_dir: Path, fmt: str, prefix: str) -> list[str]:
    saved = []
    ext = _ext_for_format(fmt)
    for i, item in enumerate(items):
        filename = f"{prefix}_{i}.{ext}" if len(items) > 1 else f"{prefix}.{ext}"
        out_path = output_dir / filename
        if "b64_json" in item:
            _save_b64(item["b64_json"], out_path)
        elif "url" in item:
            _save_url(item["url"], out_path)
        else:
            continue
        saved.append(str(out_path))
    return saved


# ---------------------------------------------------------------------------
# Sync generate (called from asyncio.to_thread)
# ---------------------------------------------------------------------------

def _run_generate_sync(args: dict) -> list[str]:
    model = args.get("model") or DEFAULT_MODEL
    prompt = args["prompt"]
    size = args.get("size", "1024x1024")
    n = max(1, min(4, args.get("n", 1)))
    quality = args.get("quality", "auto")
    fmt = args.get("output_format", "png")
    neg = args.get("negative_prompt")
    style = args.get("style_intensity", 50)
    bg = args.get("background", "auto")
    seed = args.get("seed")
    extra = args.get("extra") or {}
    output_dir = args.get("output_dir", "C:/Users/User/Claude")
    prefix = args.get("filename_prefix") or _timestamp_prefix()
    backend = (args.get("backend") or BACKEND).lower()

    out = _resolve_output_dir(output_dir)
    if backend == "images":
        items = _call_images_generate(
            model, prompt, size=size, n=n, quality=quality, fmt=fmt,
            background=bg, negative_prompt=neg, seed=seed, extra=extra,
        )
    elif backend == "responses":
        items = _call_responses_generate(
            model, prompt, size=size, n=n, quality=quality, fmt=fmt,
            background=bg, negative_prompt=neg, seed=seed, extra=extra,
        )
    elif backend == "chat":
        full_prompt = _build_prompt_with_hints(
            prompt, size=size, n=n, quality=quality,
            negative_prompt=neg, style_intensity=style, background=bg, seed=seed,
        )
        if extra:
            full_prompt += f"\n[Extra: {json.dumps(extra, ensure_ascii=False)}]"
        items = _call_chat_generate(model, full_prompt, n)
    else:
        raise RuntimeError(f"Unknown backend: {backend}")
    return _save_results(items, out, fmt, prefix)


# ---------------------------------------------------------------------------
# Sync edit (called from asyncio.to_thread)
# ---------------------------------------------------------------------------

def _run_edit_sync(args: dict) -> list[str]:
    # image_paths: list of paths, or single path string (backwards compat)
    raw = args.get("image_paths") or args.get("image_path")
    if isinstance(raw, str):
        image_paths = [raw]
    elif isinstance(raw, list):
        image_paths = raw
    else:
        raise RuntimeError("edit_image requires image_path or image_paths")
    for p in image_paths:
        if not Path(p).is_file():
            raise RuntimeError(f"Image not found: {p}")

    mask_path = args.get("mask_path")
    prompt = args["prompt"]
    model = args.get("model") or DEFAULT_MODEL
    size = args.get("size", "auto")
    n = max(1, min(10, args.get("n", 1)))
    quality = args.get("quality", "auto")
    fmt = args.get("output_format", "png")
    output_compression = args.get("output_compression")
    output_dir = args.get("output_dir", "C:/Users/User/Claude")
    prefix = args.get("filename_prefix") or f"edit_{_timestamp_prefix()}"
    backend = (args.get("backend") or BACKEND).lower()

    out = _resolve_output_dir(output_dir)
    if backend == "images":
        items = _call_images_edit(
            model, prompt, image_paths, mask_path,
            size=size, n=n, quality=quality, fmt=fmt,
            output_compression=output_compression,
        )
    elif backend == "chat":
        style = args.get("style_intensity", 50)
        full_prompt = _build_prompt_with_hints(
            prompt, size=size if size != "auto" else "1024x1024",
            n=n, quality=quality, negative_prompt=None,
            style_intensity=style, background="auto", seed=None,
        )
        items = _call_chat_edit(model, full_prompt, image_paths[0], mask_path)
    elif backend == "responses":
        raise RuntimeError("edit_image via responses backend not yet implemented")
    else:
        raise RuntimeError(f"Unknown backend: {backend}")
    return _save_results(items, out, fmt, prefix)




@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description=(
                "Submit an image generation task. Returns a task_id immediately (within seconds). "
                "Use check_task to poll for completion. Supports parallel submissions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Positive prompt describing the desired image"},
                    "model": {"type": "string", "description": "Model name (default: gpt-image-2)", "default": "gpt-image-2"},
                    "size": {"type": "string", "description": "Image size", "default": "1024x1024", "enum": list(ALLOWED_SIZES)},
                    "n": {"type": "integer", "description": "Number of images (1-4)", "default": 1, "minimum": 1, "maximum": 4},
                    "quality": {"type": "string", "description": "Quality: auto, high, medium, low", "default": "auto", "enum": list(ALLOWED_QUALITIES)},
                    "output_format": {"type": "string", "description": "Output format", "default": "png", "enum": list(ALLOWED_FORMATS)},
                    "negative_prompt": {"type": "string", "description": "Things to avoid"},
                    "style_intensity": {"type": "integer", "description": "Style intensity 0-100", "default": 50, "minimum": 0, "maximum": 100},
                    "background": {"type": "string", "description": "Background", "default": "auto", "enum": list(ALLOWED_BACKGROUNDS)},
                    "seed": {"type": "integer", "description": "Random seed"},
                    "extra": {"type": "object", "description": "Extra fields passed to API"},
                    "output_dir": {"type": "string", "description": "Absolute path to save images", "default": "C:/Users/User/Claude"},
                    "filename_prefix": {"type": "string", "description": "Custom filename prefix"},
                    "backend": {"type": "string", "description": "Backend: images/responses/chat (default from env)"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="check_task",
            description=(
                "Check the status of a generate_image task. "
                "status: pending | running | done | error. "
                "When done, saved_paths lists the generated file paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID returned by generate_image"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="list_task_history",
            description="List recent image generation tasks (up to 50). Optionally filter by status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "description": "Filter by status (default: all)",
                        "enum": ["all", "done", "error", "pending", "running"],
                        "default": "all",
                    },
                },
            },
        ),
        Tool(
            name="edit_image",
            description=(
                "Submit an image edit task using gpt-image-2. Returns a task_id immediately. "
                "Use check_task to poll for completion.\n\n"
                "Capabilities:\n"
                "- Inpainting: provide mask_path (PNG with alpha) to edit only the transparent region\n"
                "- Maskless editing: omit mask_path and describe the change in the prompt — model locates the region automatically\n"
                "- Multi-image reference: pass up to 16 image paths in image_paths for style transfer or composition\n"
                "- Style transfer: provide reference images alongside a style prompt\n"
                "- Background swap, object removal, text replacement, color change — all via prompt"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Absolute path to the primary source image (use this OR image_paths, not both)",
                    },
                    "image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of 1-16 absolute image paths. First is primary; extras are references for composition/style transfer.",
                        "minItems": 1,
                        "maxItems": 16,
                    },
                    "mask_path": {
                        "type": "string",
                        "description": (
                            "Optional. Absolute path to a PNG mask with alpha channel. "
                            "Transparent pixels = area to edit; opaque pixels = preserved. "
                            "Must be same dimensions as the source image. "
                            "Omit to let the model locate the edit region from the prompt."
                        ),
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Edit instruction, e.g. 'Replace the background with a sunset beach, keep the subject'",
                    },
                    "model": {"type": "string", "default": "gpt-image-2"},
                    "size": {
                        "type": "string",
                        "description": "Output size. Use 'auto' to match input image dimensions.",
                        "default": "auto",
                        "enum": list(ALLOWED_EDIT_SIZES),
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of output variants (1-10)",
                        "default": 1,
                        "minimum": 1,
                        "maximum": 10,
                    },
                    "quality": {"type": "string", "default": "auto", "enum": list(ALLOWED_QUALITIES)},
                    "output_format": {"type": "string", "default": "png", "enum": list(ALLOWED_FORMATS)},
                    "output_compression": {
                        "type": "integer",
                        "description": "Compression level 0-100, only applies to jpeg/webp output",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "output_dir": {"type": "string", "default": "C:/Users/User/Claude"},
                    "filename_prefix": {"type": "string"},
                    "backend": {"type": "string", "description": "Backend override: images/chat (default from env)"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="list_generated_images",
            description="List image files in a directory by glob pattern.",
            inputSchema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory to list", "default": "C:/Users/User/Claude"},
                    "pattern": {"type": "string", "description": "Glob pattern", "default": "img_*"},
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "generate_image":
        return await _handle_generate(arguments)
    elif name == "check_task":
        return await _handle_check_task(arguments)
    elif name == "list_task_history":
        return await _handle_list_history(arguments)
    elif name == "edit_image":
        return await _handle_edit(arguments)
    elif name == "list_generated_images":
        return await _handle_list(arguments)
    else:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def _handle_generate(args: dict) -> list[TextContent]:
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "type": "generate",
        "status": "pending",
        "created_at": time.time(),
        "finished_at": None,
        "args": args,
        "result": None,
        "error": None,
    }
    _task_store[task_id] = task
    _ensure_worker()
    result = {
        "task_id": task_id,
        "status": "pending",
        "message": "Task submitted. Use check_task to poll for completion.",
    }
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def _handle_check_task(args: dict) -> list[TextContent]:
    task_id = args.get("task_id", "")
    task = _task_store.get(task_id)
    if task is None:
        return [TextContent(type="text", text=json.dumps({"error": f"Task not found: {task_id}"}))]
    result: dict = {
        "task_id": task_id,
        "status": task["status"],
        "created_at": task["created_at"],
        "finished_at": task["finished_at"],
    }
    if task["status"] == "done":
        result["saved_paths"] = task["result"]
        if task["finished_at"]:
            result["elapsed_seconds"] = round(task["finished_at"] - task["created_at"], 1)
    elif task["status"] == "error":
        result["error"] = task["error"]
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def _handle_list_history(args: dict) -> list[TextContent]:
    status_filter = args.get("status_filter", "all")
    entries = list(_task_history)
    if status_filter != "all":
        entries = [t for t in entries if t["status"] == status_filter]
    output = []
    for t in reversed(entries):  # most recent first
        entry: dict = {
            "task_id": t["id"],
            "status": t["status"],
            "created_at": t["created_at"],
            "finished_at": t["finished_at"],
        }
        if t["status"] == "done":
            entry["saved_paths"] = t["result"]
            if t["finished_at"]:
                entry["elapsed_seconds"] = round(t["finished_at"] - t["created_at"], 1)
        elif t["status"] == "error":
            entry["error"] = t["error"]
        output.append(entry)
    return [TextContent(type="text", text=json.dumps({"count": len(output), "tasks": output}, ensure_ascii=False, indent=2))]


async def _handle_edit(args: dict) -> list[TextContent]:
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "type": "edit",
        "status": "pending",
        "created_at": time.time(),
        "finished_at": None,
        "args": args,
        "result": None,
        "error": None,
    }
    _task_store[task_id] = task
    _ensure_worker()
    result = {
        "task_id": task_id,
        "status": "pending",
        "message": "Edit task submitted. Use check_task to poll for completion.",
    }
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def _handle_list(args: dict) -> list[TextContent]:
    directory = args.get("directory", "C:/Users/User/Claude")
    pattern = args.get("pattern", "img_*")
    d = Path(directory)
    if not d.is_dir():
        return [TextContent(type="text", text=json.dumps({"error": f"Not a directory: {directory}"}))]
    files = sorted(d.glob(pattern))
    result = {"directory": directory, "pattern": pattern, "count": len(files), "files": [str(f) for f in files]}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
