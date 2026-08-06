"""
etna/server.py — Etna Protocol + MCP JSON-RPC server

Endpoints:
  GET  /list_kits              All installed kit names and descriptions
  POST /inspect_kit            Kit metadata (name, description, tool count, skill)
  POST /list_tools_in_kit      Full tool schemas for a kit
  POST /inspect_tool           Schema for a single tool
  POST /run_tool               Execute a tool, returns result directly
  POST /reload_kit             Hot-reload a kit without restart
  POST /search_tools           Keyword search across all tool names/descriptions

  GET  /list_skills            All installed general skills (name + description)
  POST /read_skill             SKILL.md body for any skill (kit or general)

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
KITS_DIR        = cfg.KITS_DIR

# Auto-discover all kits
_etna_pkg_parent = str(Path(__file__).resolve().parent.parent)
if _etna_pkg_parent not in sys.path:
    sys.path.insert(0, _etna_pkg_parent)
if str(cfg.CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(cfg.CONFIG_DIR))

cfg.kits_dir()

import glob as _glob
_venv_site = _glob.glob(str(cfg.VENV_DIR / "lib" / "python*" / "site-packages"))
for _p in _venv_site:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kits  # noqa: F401


def _apply_kit_configs():
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("kits."):
            continue
        kit_stem = mod_name.split(".", 1)[1]
        if not hasattr(mod, "config") or not isinstance(mod.config, dict):
            continue
        saved = cfg.load_kit_config(kit_stem)
        if saved:
            mod.config.update(saved)
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


# ── Skill helpers ─────────────────────────────────────────────────────────────

def _parse_skill_meta(skill_dir: Path) -> dict | None:
    """
    Parse the frontmatter from a skill's SKILL.md.
    Returns {name, description} or None if SKILL.md is missing or malformed.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")

    # Parse YAML frontmatter between --- delimiters
    name = skill_dir.name
    description = ""

    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            frontmatter = content[3:end].strip()
            for line in frontmatter.splitlines():
                if line.startswith("name:"):
                    name = line[5:].strip()
                elif line.startswith("description:"):
                    description = line[12:].strip()

    return {"name": name, "description": description}


def _skill_body(skill_dir: Path) -> str | None:
    """Return the body of a SKILL.md (everything after the frontmatter)."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")

    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3:].strip()

    return content.strip()


def _kit_skill_dir(kit_stem: str) -> Path | None:
    """Return the skill dir for a kit if it exists, else None."""
    path = cfg.kit_skill_path(kit_stem)
    if path.exists() and (path / "SKILL.md").exists():
        return path
    return None


def _general_skill_dir(skill_name: str) -> Path | None:
    """Return the general skill dir by name if it exists, else None."""
    # Search by folder name and by SKILL.md name field
    skills_root = cfg.SKILLS_DIR
    if not skills_root.exists():
        return None
    for skill_dir in skills_root.iterdir():
        if not skill_dir.is_dir():
            continue
        if skill_dir.name == skill_name:
            return skill_dir
        meta = _parse_skill_meta(skill_dir)
        if meta and meta["name"] == skill_name:
            return skill_dir
    return None


# ── Etna Protocol endpoints ───────────────────────────────────────────────────

@app.get("/list_kits")
def list_kits():
    return {"kits": [m["kit_name"] for m in _all_kit_metas()]}


@app.post("/inspect_kit")
def inspect_kit(req: dict):
    kit_name = req.get("kit")
    if not kit_name:
        return JSONResponse({"error": "Missing 'kit' field"}, status_code=400)

    for meta in _all_kit_metas():
        if meta["kit_name"] == kit_name:
            stem = Path(meta["filename"]).stem
            skill_dir = _kit_skill_dir(stem)
            skill_meta = _parse_skill_meta(skill_dir) if skill_dir else None
            meta["skill"] = skill_meta["name"] if skill_meta else None
            return meta

    return JSONResponse({"error": f"Kit '{kit_name}' not found"}, status_code=404)


@app.post("/list_tools_in_kit")
def list_tools_in_kit(req: dict):
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
    stem = req.get("kit_stem")
    if not stem:
        kit_name = req.get("kit")
        if kit_name:
            stem = _kit_stem_for_name(kit_name)
    if not stem:
        return JSONResponse({"error": "Provide 'kit_stem' or 'kit' field"}, status_code=400)

    module_name = f"kits.{stem}"

    stale = [name for name, func in list(TOOLS.items())
             if getattr(func, "_kit", None) == stem]
    for name in stale:
        TOOLS.pop(name, None)

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
    stem = req.get("kit_stem")
    if not stem:
        return JSONResponse({"error": "Provide 'kit_stem' field"}, status_code=400)

    removed = [name for name, func in list(TOOLS.items())
               if getattr(func, "_kit", None) == stem]
    for name in removed:
        TOOLS.pop(name, None)

    module_name = f"kits.{stem}"
    sys.modules.pop(module_name, None)

    return {"kit_stem": stem, "tools_removed": removed}


@app.post("/search_tools")
def search_tools(req: dict):
    query = req.get("query", "").lower().strip()
    if not query:
        return JSONResponse({"error": "Missing 'query' field"}, status_code=400)

    keywords = query.split()
    tools = get_tools()
    results = []

    for tool_name, func in tools.items():
        doc = (func.__doc__ or "").lower()
        searchable = f"{tool_name.lower()} {doc}"
        score = sum(1 for kw in keywords if kw in searchable)
        if score > 0:
            results.append({
                "kit": getattr(func, "_kit", None),
                "tool": tool_name,
                "_score": score,
            })

    results.sort(key=lambda x: x["_score"], reverse=True)
    return {
        "results": [{"kit": r["kit"], "tool": r["tool"]} for r in results]
    }


# ── Skill endpoints ───────────────────────────────────────────────────────────

@app.get("/list_skills")
def list_skills():
    """
    Return all installed general skills (name + description).
    Kit skills are not included here — they are surfaced via inspect_kit.
    """
    skills_root = cfg.SKILLS_DIR
    if not skills_root.exists():
        return {"skills": []}

    result = []
    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        meta = _parse_skill_meta(skill_dir)
        if meta:
            result.append(meta)

    return {"skills": result}


@app.post("/read_skill")
def read_skill(req: dict):
    """
    Return the SKILL.md body for any skill — general or kit.
    Request:  { "skill": "<name>" }
    Response: { "name": str, "body": str }

    Searches general skills first, then kit skills.
    """
    skill_name = req.get("skill")
    if not skill_name:
        return JSONResponse({"error": "Missing 'skill' field"}, status_code=400)

    # Check general skills first
    skill_dir = _general_skill_dir(skill_name)

    # Fall back to kit skills
    if skill_dir is None:
        kit_skills_root = cfg.KIT_SKILLS_DIR
        if kit_skills_root.exists():
            for candidate in kit_skills_root.iterdir():
                if not candidate.is_dir():
                    continue
                meta = _parse_skill_meta(candidate)
                if meta and meta["name"] == skill_name:
                    skill_dir = candidate
                    break
                # Also match by folder name (kit stem)
                if candidate.name == skill_name:
                    skill_dir = candidate
                    break

    if skill_dir is None:
        return JSONResponse({"error": f"Skill '{skill_name}' not found"}, status_code=404)

    body = _skill_body(skill_dir)
    if body is None:
        return JSONResponse({"error": f"SKILL.md missing for '{skill_name}'"}, status_code=404)

    return {"name": skill_name, "body": body}


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
    return await _mcp_sse_handler(request)


@app.post("/mcp")
async def mcp_jsonrpc_all(request: Request):
    return await _mcp_jsonrpc_handler(request, kit_stem=None)


@app.get("/mcp/{kit_stem}")
async def mcp_sse_kit(kit_stem: str, request: Request):
    return await _mcp_sse_handler(request)


@app.post("/mcp/{kit_stem}")
async def mcp_jsonrpc_kit(kit_stem: str, request: Request):
    return await _mcp_jsonrpc_handler(request, kit_stem=kit_stem)
