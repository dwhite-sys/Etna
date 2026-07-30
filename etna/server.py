"""
etna/server.py — Etna Protocol + MCP JSON-RPC server

Endpoints:
  GET  /list_kits              All installed kit names and descriptions
  POST /inspect_kit            Kit metadata (name, description, tool count)
  POST /list_tools_in_kit      Full tool schemas for a kit
  POST /inspect_tool           Schema for a single tool
  POST /run_tool               Execute a tool, returns result directly
  POST /reload_kit             Hot-reload a kit without restart
  POST /search_tools           Keyword search across all tool names/descriptions

  POST /mcp                    MCP JSON-RPC 2.0 (initialize, tools/list, tools/call)
  GET  /mcp                    SSE keepalive stream
"""

import ast
import sys
import json
import asyncio
import os
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from etna.utils.registry import (
    get_tools, get_tools_for_kit, extract_parameters, build_tool_schema, TOOLS
)
from etna import config as cfg

_executor = ThreadPoolExecutor()

KIT_CONFIGS_DIR = cfg.KIT_CONFIGS_DIR
KITS_DIR = cfg.KITS_DIR

# Auto-discover all kits
# cfg.CONFIG_DIR  → so "import kits" resolves (~/.etna_server/ contains kits/)
# parent of etna/ → so "from utils import tool" in kits resolves to etna/utils/
_etna_pkg_parent = str(Path(__file__).resolve().parent.parent)
if _etna_pkg_parent not in sys.path:
    sys.path.insert(0, _etna_pkg_parent)
if str(cfg.CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(cfg.CONFIG_DIR))

# Ensure kits dir and its __init__.py exist before importing
cfg.kits_dir()

# Add venv site-packages to sys.path so kits can do "from utils import tool"
import glob as _glob
_venv_site = _glob.glob(str(cfg.VENV_DIR / "lib" / "python*" / "site-packages"))
for _p in _venv_site:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kits  # noqa: F401


def _apply_kit_configs():
    """
    Mutate each kit module's config dict with saved values from
    ~/.etna/kit_configs/<kit_stem>/config.json.
    User-set values take precedence over kit defaults.
    """
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("kits."):
            continue
        kit_stem = mod_name.split(".", 1)[1]
        if not hasattr(mod, "config") or not isinstance(mod.config, dict):
            continue
        saved = cfg.load_kit_config(kit_stem)
        if saved:
            mod.config.update(saved)
        # Inject config values as environment variables so os.getenv() works too
        for key, val in mod.config.items():
            os.environ.setdefault(key, str(val))
        if saved:
            for key, val in saved.items():
                os.environ[key] = str(val)


_apply_kit_configs()

app = FastAPI(title="Etna Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Kit metadata helpers ──────────────────────────────────────────────────────

def _parse_kit_meta(kit_path: Path) -> dict:
    meta = {
        "kit_name": kit_path.stem,
        "kit_description": "",
        "filename": kit_path.name,
        "enabled": True,
    }
    try:
        tree = ast.parse(kit_path.read_text(encoding="utf-8"))
    except Exception:
        return meta

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "kit_name" and isinstance(node.value, ast.Constant):
                meta["kit_name"] = node.value.value
            elif target.id == "kit_description" and isinstance(node.value, ast.Constant):
                meta["kit_description"] = node.value.value
    return meta


def _all_kit_metas() -> list[dict]:
    metas = []
    if not KITS_DIR.exists():
        return metas
    for kit_file in sorted(KITS_DIR.glob("*.py")):
        if kit_file.name.startswith("_"):
            continue
        metas.append(_parse_kit_meta(kit_file))
    return metas


def _kit_stem_for_name(kit_name: str) -> str | None:
    for kit_file in KITS_DIR.glob("*.py"):
        if kit_file.name.startswith("_"):
            continue
        meta = _parse_kit_meta(kit_file)
        if meta["kit_name"] == kit_name:
            return kit_file.stem
    return None


# ── Etna Protocol endpoints ───────────────────────────────────────────────────

@app.get("/list_kits")
def list_kits():
    """
    Return the list of installed kits.
    Response: { "kits": ["ntfy", "Obsidian", ...] }
    """
    return {"kits": [m["kit_name"] for m in _all_kit_metas()]}


@app.post("/inspect_kit")
def inspect_kit(req: dict):
    """
    Return metadata for a single kit by kit_name.
    Request:  { "kit": "ntfy" }
    Response: { "kit_name", "kit_description", "filename", "enabled" }
    """
    kit_name = req.get("kit")
    if not kit_name:
        return JSONResponse({"error": "Missing 'kit' field"}, status_code=400)

    for meta in _all_kit_metas():
        if meta["kit_name"] == kit_name:
            return meta

    return JSONResponse({"error": f"Kit '{kit_name}' not found"}, status_code=404)


@app.post("/list_tools_in_kit")
def list_tools_in_kit(req: dict):
    """
    Return the full tool schema list for a kit.
    Request:  { "kit": "ntfy" }
    Response: { "kit": "ntfy", "tools": [ { name, description, parameters }, ... ] }
    """
    kit_name = req.get("kit")
    if not kit_name:
        return JSONResponse({"error": "Missing 'kit' field"}, status_code=400)

    stem = _kit_stem_for_name(kit_name)
    if stem is None:
        return JSONResponse({"error": f"Kit '{kit_name}' not found"}, status_code=404)

    kit_tools = get_tools_for_kit(stem)
    return {
        "kit": kit_name,
        "tools": [build_tool_schema(name, func) for name, func in kit_tools.items()],
    }


@app.post("/inspect_tool")
def inspect_tool(req: dict):
    """
    Return the schema for a single tool by function name.
    Request:  { "tool": "ntfy_send" }
    Response: { name, description, parameters, kit }
    """
    tool_name = req.get("tool")
    if not tool_name:
        return JSONResponse({"error": "Missing 'tool' field"}, status_code=400)

    tools = get_tools()
    if tool_name not in tools:
        return JSONResponse({"error": f"Tool '{tool_name}' not found"}, status_code=404)

    func = tools[tool_name]
    schema = build_tool_schema(tool_name, func)
    schema["kit"] = getattr(func, "_kit", None)
    return schema


@app.post("/run_tool")
async def run_tool(req: dict):
    """
    Execute a tool by name with JSON arguments.
    Request:  { "tool": "ntfy_send", "arguments": {...} }
    Response: { "result": ... } or { "error": ... }
    """
    name = req.get("tool")
    args = req.get("arguments", {})

    tools = get_tools()
    if name not in tools:
        return {"error": f"Tool '{name}' not found"}

    func = tools[name]
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_executor, lambda: func(**args))
        return {"result": result}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.post("/reload_kit")
def reload_kit(req: dict):
    """
    Dynamically import (or re-import) a kit module so its @tool decorators
    fire and register tools without restarting the server.

    Request:  { "kit_stem": "nfty_kit" } OR { "kit": "ntfy" }
    Response: { "kit_stem": str, "tools_registered": [str, ...] }
    """
    stem = req.get("kit_stem")
    if not stem:
        kit_name = req.get("kit")
        if kit_name:
            stem = _kit_stem_for_name(kit_name)
    if not stem:
        return JSONResponse({"error": "Provide 'kit_stem' or 'kit' field"}, status_code=400)

    module_name = f"kits.{stem}"

    # Remove stale tool registrations for this kit
    stale = [name for name, func in list(TOOLS.items())
             if getattr(func, "_kit", None) == stem]
    for name in stale:
        TOOLS.pop(name, None)

    # Clear bytecode so edits are picked up
    pycache = KITS_DIR / "__pycache__"
    for f in pycache.glob(f"{stem}*.pyc"):
        f.unlink(missing_ok=True)

    try:
        if module_name in sys.modules:
            mod = sys.modules[module_name]
            importlib.reload(mod)
        else:
            importlib.import_module(module_name)
    except Exception as exc:
        return JSONResponse({"error": f"Failed to import {module_name}: {exc}"}, status_code=500)

    _apply_kit_configs()

    registered = list(get_tools_for_kit(stem).keys())
    return {"kit_stem": stem, "tools_registered": registered}


@app.post("/unload_kit")
def unload_kit(req: dict):
    """
    Remove a kit's tools from the registry without restarting the server.
    Called when a kit is removed while the server is running.

    Request:  { "kit_stem": "nfty_kit" }
    Response: { "kit_stem": str, "tools_removed": [str, ...] }
    """
    stem = req.get("kit_stem")
    if not stem:
        return JSONResponse({"error": "Provide 'kit_stem' field"}, status_code=400)

    removed = [name for name, func in list(TOOLS.items())
               if getattr(func, "_kit", None) == stem]
    for name in removed:
        TOOLS.pop(name, None)

    # Also remove from sys.modules so reimport is clean if kit is reinstalled
    module_name = f"kits.{stem}"
    sys.modules.pop(module_name, None)

    return {"kit_stem": stem, "tools_removed": removed}


@app.post("/search_tools")
def search_tools(req: dict):
    """
    Keyword search across all tool names and docstrings.
    Returns matching tools with their kit name.

    Request:  { "query": "send notification" }
    Response: { "results": [ { "kit": str, "tool": str }, ... ] }
    """
    query = req.get("query", "").lower().strip()
    if not query:
        return JSONResponse({"error": "Missing 'query' field"}, status_code=400)

    keywords = query.split()
    tools = get_tools()
    results = []

    for tool_name, func in tools.items():
        doc = (func.__doc__ or "").lower()
        searchable = f"{tool_name.lower()} {doc}"

        # Score by how many keywords match
        score = sum(1 for kw in keywords if kw in searchable)
        if score > 0:
            results.append({
                "kit": getattr(func, "_kit", None),
                "tool": tool_name,
                "_score": score,
            })

    # Sort by score descending
    results.sort(key=lambda x: x["_score"], reverse=True)

    # Strip internal score from output
    return {
        "results": [{"kit": r["kit"], "tool": r["tool"]} for r in results]
    }


# ── MCP JSON-RPC 2.0 ─────────────────────────────────────────────────────────

def _mcp_tool_schema(name: str, func) -> dict:
    return {
        "name": name,
        "description": func.__doc__ or "",
        "inputSchema": extract_parameters(func),
    }


def _sse_message(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def _mcp_sse_handler(request: Request):
    """SSE keepalive stream for MCP clients."""
    async def event_stream():
        while True:
            if await request.is_disconnected():
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(15)
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _mcp_jsonrpc_handler(request: Request, kit_stem: str | None = None):
    """
    MCP JSON-RPC 2.0 dispatch.
    Handles: initialize, tools/list, tools/call
    kit_stem scopes the tool surface to a single kit when provided.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    req_id    = body.get("id")
    method    = body.get("method", "")
    params    = body.get("params") or {}
    accept    = request.headers.get("accept", "")
    wants_sse = "text/event-stream" in accept

    def _tools() -> dict:
        return get_tools_for_kit(kit_stem) if kit_stem else get_tools()

    def make_response(result: dict):
        payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
        if wants_sse:
            return StreamingResponse(
                iter([_sse_message(payload)]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(payload)

    def make_error(code: int, message: str):
        payload = {"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": code, "message": message}}
        if wants_sse:
            return StreamingResponse(
                iter([_sse_message(payload)]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(payload)

    if method.startswith("notifications/"):
        return JSONResponse(status_code=202, content=None)

    if method == "initialize":
        return make_response({
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Etna", "version": "1.0.0-beta"},
        })

    elif method == "tools/list":
        tools = _tools()
        return make_response({
            "tools": [_mcp_tool_schema(n, f) for n, f in tools.items()]
        })

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        tools     = _tools()

        if tool_name not in tools:
            return make_error(-32601, f"Tool '{tool_name}' not found")

        try:
            loop   = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _executor, lambda: tools[tool_name](**arguments)
            )
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)
            return make_response({
                "content": [{"type": "text", "text": result}],
                "isError": False,
            })
        except Exception as e:
            return make_response({
                "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                "isError": True,
            })

    else:
        return make_error(-32601, f"Method not found: {method}")


# ── MCP routes — unscoped and kit-scoped ─────────────────────────────────────

@app.get("/mcp")
async def mcp_sse_all(request: Request):
    """SSE keepalive — all kits."""
    return await _mcp_sse_handler(request)


@app.post("/mcp")
async def mcp_jsonrpc_all(request: Request):
    """MCP JSON-RPC — all kits."""
    return await _mcp_jsonrpc_handler(request, kit_stem=None)


@app.get("/mcp/{kit_stem}")
async def mcp_sse_kit(kit_stem: str, request: Request):
    """SSE keepalive — scoped to a single kit."""
    return await _mcp_sse_handler(request)


@app.post("/mcp/{kit_stem}")
async def mcp_jsonrpc_kit(kit_stem: str, request: Request):
    """MCP JSON-RPC — scoped to a single kit."""
    return await _mcp_jsonrpc_handler(request, kit_stem=kit_stem)
