# Etna

A tool server built for agentic harnesses. Etna gives you tool isolation out of the box — each kit is its own branch of the tool tree, intended to be independently toggleable per session for harnesses, without needing multiple concurrent server processes to manage. Drop a Python file in, and it's live.

Kit authoring is intentionally Pythonic: a decorator, type hints, and a docstring are all the model needs to understand and use a tool. Everything else — schema generation, dependency management, client registration, hot-reload — is handled for you.

## Why Etna instead of MCP

MCP solves the problem of getting tools into models, and it does that well. What it doesn't solve is what happens when you have a lot of them. The standard answer is to run a separate server process per tool category and register each one individually in your client config. That works, but it means your tool surface area is defined at the process level — adding a capability requires restarting something, and toggling access requires editing a config file.

Etna takes a different position. One server, one connection, all your kits. Isolation happens at the kit level inside that single process — each kit is its own independently togglable branch of the tool tree. A harness can expose different kits to different models, different sessions, or different users, all from the same running server. No additional processes, no config restarts.

MCP compatibility ships as an adapter so Etna works with any client that already speaks MCP. But the Etna Protocol is its own thing — built directly on LLM tool use, not on top of MCP.

## How it works

- **CLI (`etna`)** — manages the full lifecycle. Install kits, start the server, configure clients, manage Chrome for browser automation. One command for everything.
- **Kits (`~/.etna_server/kits/`)** — each kit is a plain Python file with `@tool`-decorated functions and four metadata fields. Drop one in, and it's live.
- **Server** — FastAPI process that auto-discovers all kits at startup and again on hot-reload. Speaks both the Etna Protocol and MCP JSON-RPC on the same port.
- **Etna Protocol** — a tree-based tool discovery protocol in the same category as MCP, not derived from it. Built directly on LLM tool use. The server is the root. Kits are branches. Tools are leaves. Clients walk the tree, toggle branches on and off per session, and search across tools by keyword. MCP compatibility is an adapter that ships with Etna — it's how Etna connects to clients that already speak MCP, not the foundation the protocol is built on.
- **Registry (`etna/utils/registry.py`)** — the `@tool` decorator registers functions into a global dict at import time. Type hints are scraped to build JSON Schema automatically. Docstrings become the tool description shown to the model — write them for the model, not for humans.
- **Config (`~/.etna_server/`)** — kit installs, port assignments, client registrations, and per-kit config values all live here. Written atomically, read at runtime.

## Kit anatomy

```python
# my_kit.py
from utils import tool
import os

kit_name        = "My Kit"
kit_description = "What this kit does."
requirements    = ["requests"]
config          = {"MY_VAR": "default_value"}

@tool
def do_something(query: str, limit: int = 10) -> dict:
    """
    WHEN TO USE: one sentence describing the task this solves.

    query: what to search for.
    limit: max results (default 10).

    Returns {"results": list, "count": int}.
    """
    my_var = os.getenv("MY_VAR", "default_value")
    return {"results": [], "count": 0}
```

- `requirements` is AST-parsed before the file is ever imported — no import needed, missing packages are installed automatically into Etna's managed venv.
- `config` is a dict of `{"VAR": "default"}` pairs. Values are stored in `~/.etna_server/kit_configs/<kit_stem>/config.json` and exposed as environment variables at runtime. User-set values are preserved across reinstalls.
- `@tool` is the only import required. Type hints are mandatory — they build the JSON Schema. Parameters without defaults are required, parameters with defaults are optional.

## Setup

```
pip install etna-mcp
etna install
```

`etna install` with no arguments checks whether everything is in place and sets it up if not — creates the managed venv via UV, installs server dependencies, and registers a boot service so Etna starts automatically. Running it again after setup just shows you the getting-started hints.

UV is installed automatically as a dependency.

## Adding a kit

```
etna install my_kit.py
```

If the server is already running, the kit is linted, installed, and hot-reloaded immediately — no restart needed. OpenWebUI can reach it at `http://localhost:8467/mcp/<kit_stem>` straight away.

To install from the curated kit repo:

```
etna install ntfy
```

## CLI reference

```
etna install                          First-time setup, or getting-started hints if ready
etna install <path/kit.py>            Install a kit from a local file
etna install <kit_name>               Install a kit from the curated repo
etna update <path/kit.py>            Update a kit (no prompt)
etna update <kit_name>               Update a kit from the repo (no prompt)
etna update --all                    Update all installed kits from the repo
etna remove <kit_name>               Remove a kit
etna list                            List installed kits and server status
etna status                          Server, browser, kits, and client summary

etna start                           Start the server
etna start --verbose                 Start with full log output
etna start stdio [kit_name]          Start stdio shim — scopes to /mcp/<kit_name> if given
etna stop                            Stop the server
etna restart                         Restart the server

etna compat                          Auto-detect and configure all clients
etna compat claude                   Write kit entries to Claude Desktop config
etna compat lmstudio                 Write kit entries to LM Studio config
etna compat openwebui <url> <key>    Register kits with OpenWebUI

etna config list <kit>               List config variables for a kit
etna config get <kit> <var>          Get a config value
etna config set <kit> <var> <val>    Set a config value
etna config reset <kit> <var>        Reset a config value to its default

etna browser start                   Launch Chrome with CDP for browser automation
etna browser stop                    Stop Chrome
etna browser status                  Show Chrome CDP status
```

## Etna Protocol endpoints

| Endpoint             | Method | Description                                   |
|----------------------|--------|-----------------------------------------------|
| `/list_kits`         | GET    | All installed kit names and descriptions      |
| `/inspect_kit`       | POST   | Kit metadata (name, description, tool count)  |
| `/list_tools_in_kit` | POST   | Full tool schemas for a kit                   |
| `/inspect_tool`      | POST   | Schema for a single tool                      |
| `/run_tool`          | POST   | Execute a tool, returns result directly       |
| `/reload_kit`        | POST   | Hot-reload a kit without restart              |
| `/unload_kit`        | POST   | Remove a kit's tools from the registry        |
| `/search_tools`      | POST   | Keyword search across all tool names and docs |
| `/mcp`               | POST   | MCP JSON-RPC 2.0 — all kits                   |
| `/mcp`               | GET    | SSE keepalive stream — all kits               |
| `/mcp/<kit_stem>`    | POST   | MCP JSON-RPC 2.0 — scoped to one kit          |
| `/mcp/<kit_stem>`    | GET    | SSE keepalive stream — scoped to one kit      |

## SDK

```python
from etna.sdk import lint_kit, parse_kit_metadata

result = lint_kit("my_kit.py")
# {"passed": bool, "errors": [...], "warnings": [...]}

meta = parse_kit_metadata("my_kit.py")
# {"kit_name": str, "kit_description": str, "requirements": [...], "config": {...}}
```

## License

Apache 2.0
