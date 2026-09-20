"""
etna/server.py — Etna Protocol + MCP JSON-RPC server

Endpoints:
  GET  /list_kits              All installed kit names and descriptions
  POST /inspect_kit            Kit metadata (name, description, tool count, skill)
  POST /list_tools_in_kit      Full tool schemas for a kit
  POST /inspect_tool           Schema for a single tool
  POST /run_tool               Execute a tool, returns result directly
  POST /reload_kit             Hot-reload a kit without restart

  GET  /list_skills            Installed standalone skills + stable source identity
  POST /read_skill             SKILL.md body for any skill (kit or standalone)
  POST /list_skill_files       Recursively list files in a skill package
  POST /read_skill_file        Read a text/binary file from a skill package

  POST /mcp                    MCP JSON-RPC 2.0 (initialize, tools/list, tools/call)
  GET  /mcp                    SSE keepalive stream
"""

import ast
import sys
import json
import asyncio
import os
import importlib
import base64
import mimetypes
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
        previous_env_keys = set(getattr(mod, "_etna_config_env_keys", ()))

        if not hasattr(mod, "config") or not isinstance(mod.config, dict):
            for key in previous_env_keys:
                os.environ.pop(key, None)
            mod._etna_config_env_keys = ()
            continue

        # Kit source owns the schema/defaults. Disk stores overrides only.
        defaults = getattr(mod, "_etna_config_defaults", None)
        if not isinstance(defaults, dict):
            defaults = dict(mod.config)
            mod._etna_config_defaults = dict(defaults)

        raw_saved = cfg.load_kit_config(kit_stem)
        saved = {
            key: value
            for key, value in raw_saved.items()
            if key in defaults and value != defaults[key]
        }
        if saved != raw_saved:
            cfg.save_kit_config(kit_stem, saved)

        effective = dict(defaults)
        effective.update(saved)
        mod.config.clear()
        mod.config.update(effective)

        current_env_keys = set(effective)
        for key in previous_env_keys - current_env_keys:
            os.environ.pop(key, None)
        for key, val in effective.items():
            os.environ[key] = str(val)

            # Compatibility for older kits that snapshot config globals.
            if key in mod.__dict__:
                mod.__dict__[key] = val

        mod._etna_config_env_keys = tuple(current_env_keys)


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
    """Parse a skill's SKILL.md frontmatter."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    content = skill_md.read_text(encoding="utf-8")
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
    """Return the body of SKILL.md, excluding frontmatter."""
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
    path = cfg.kit_skill_path(kit_stem)
    if path.exists() and (path / "SKILL.md").exists():
        return path
    return None


def _skill_records() -> list[dict]:
    records = []

    if cfg.SKILLS_DIR.exists():
        for skill_dir in sorted(cfg.SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                continue
            meta = _parse_skill_meta(skill_dir)
            if meta:
                records.append({
                    **meta,
                    "source": "skills",
                    "stem": skill_dir.name,
                    "_path": skill_dir,
                })

    if cfg.KIT_SKILLS_DIR.exists():
        for skill_dir in sorted(cfg.KIT_SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir() or skill_dir.is_symlink():
                continue
            meta = _parse_skill_meta(skill_dir)
            if meta:
                records.append({
                    **meta,
                    "source": f"kits/{skill_dir.name}",
                    "stem": skill_dir.name,
                    "_path": skill_dir,
                })

    return records


def _resolve_skill(skill_name: str, source: str | None = None):
    records = _skill_records()
    if source:
        records = [
            record for record in records
            if record["source"] == source
        ]

    matches = [
        record for record in records
        if record["name"] == skill_name
        or record["stem"] == skill_name
    ]

    if not matches:
        return None, JSONResponse(
            {"error": f"Skill '{skill_name}' not found"},
            status_code=404,
        )

    if len(matches) > 1 and not source:
        return None, JSONResponse(
            {
                "error": f"Skill '{skill_name}' exists in multiple sources",
                "sources": [record["source"] for record in matches],
            },
            status_code=409,
        )

    return matches[0], None


def _safe_skill_file(skill_root: Path, relative_path: str):
    requested = Path(relative_path)
    if requested.is_absolute() or requested.drive or ".." in requested.parts:
        return None

    root = skill_root.resolve()
    target = (root / requested).resolve()

    try:
        target.relative_to(root)
    except ValueError:
        return None

    if not target.is_file():
        return None

    return target

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
            meta["skill_source"] = f"kits/{stem}" if skill_meta else None
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
            previous_env_keys = tuple(getattr(mod, "_etna_config_env_keys", ()))
            previous_config_keys = tuple(
                mod.config.keys()
                if isinstance(getattr(mod, "config", None), dict)
                else ()
            )

            mod.__dict__.pop("_etna_config_defaults", None)
            mod.__dict__.pop("config", None)
            for key in previous_config_keys:
                mod.__dict__.pop(key, None)

            importlib.reload(mod)
            mod._etna_config_env_keys = previous_env_keys
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


# Runtime ranking/search belongs to the client/harness. Etna exposes
# deterministic registry and package primitives only.

# ── Skill endpoints ───────────────────────────────────────────────────────────

@app.get("/list_skills")
def list_skills():
    result = []

    for record in _skill_records():
        if record["source"] != "skills":
            continue

        result.append({
            "name": record["name"],
            "description": record["description"],
            "source": record["source"],
            "stem": record["stem"],
        })

    return {"skills": result}


@app.post("/read_skill")
def read_skill(req: dict):
    skill_name = req.get("skill")
    source = req.get("source")

    if not skill_name:
        return JSONResponse({"error": "Missing 'skill' field"}, status_code=400)

    record, error = _resolve_skill(skill_name, source)
    if error:
        return error

    body = _skill_body(record["_path"])
    if body is None:
        return JSONResponse(
            {"error": f"SKILL.md missing for '{skill_name}'"},
            status_code=404,
        )

    return {
        "name": record["name"],
        "source": record["source"],
        "body": body,
    }


@app.post("/list_skill_files")
def list_skill_files(req: dict):
    skill_name = req.get("skill")
    source = req.get("source")

    if not skill_name:
        return JSONResponse({"error": "Missing 'skill' field"}, status_code=400)

    record, error = _resolve_skill(skill_name, source)
    if error:
        return error

    root = record["_path"].resolve()
    files = []

    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue

        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue

        files.append(candidate.relative_to(root).as_posix())

    files = sorted(
        set(files),
        key=lambda path: (path != "SKILL.md", path),
    )

    return {
        "skill": record["name"],
        "source": record["source"],
        "files": files,
    }


@app.post("/read_skill_file")
def read_skill_file(req: dict):
    skill_name = req.get("skill")
    source = req.get("source")
    relative_path = req.get("file")

    if not skill_name:
        return JSONResponse({"error": "Missing 'skill' field"}, status_code=400)

    if not relative_path:
        return JSONResponse({"error": "Missing 'file' field"}, status_code=400)

    record, error = _resolve_skill(skill_name, source)
    if error:
        return error

    target = _safe_skill_file(
        record["_path"],
        str(relative_path),
    )

    if target is None:
        return JSONResponse(
            {"error": f"File '{relative_path}' not found"},
            status_code=404,
        )

    data = target.read_bytes()
    response = {
        "skill": record["name"],
        "source": record["source"],
        "file": str(relative_path).replace("\\", "/"),
    }

    try:
        response["content"] = data.decode("utf-8")
        response["binary"] = False
    except UnicodeDecodeError:
        response["binary"] = True
        response["contentType"] = (
            mimetypes.guess_type(target.name)[0]
            or "application/octet-stream"
        )
        response["base64"] = base64.b64encode(data).decode("ascii")

    return response

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
