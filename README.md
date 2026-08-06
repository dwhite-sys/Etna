# Etna

A tool server built for agentic harnesses. Etna gives you tool isolation out of the box — each kit is its own branch of the tool tree, independently toggleable per session. Drop a Python file in, and it's live.

Kit authoring is intentionally Pythonic: a decorator, type hints, and a docstring are all the model needs to understand and use a tool. Everything else — schema generation, dependency management, client registration, hot-reload — is handled for you.

## Why Etna instead of MCP

MCP solves the problem of getting tools into models, and it does that well. What it doesn't solve is what happens when you have a lot of them. The standard answer is to run a separate server process per tool category and register each one individually in your client config. That works, but it means your tool surface area is defined at the process level — adding a capability requires restarting something, and toggling access requires editing a config file.

Etna takes a different position. One server, one connection, all your kits. Isolation happens at the kit level inside that single process — each kit is its own independently toggleable branch of the tool tree. A harness can expose different kits to different models, different sessions, or different users, all from the same running server. No additional processes, no config restarts.

MCP compatibility ships as an adapter so Etna works with any client that already speaks MCP. But the Etna Protocol is its own thing — built directly on LLM tool use, not on top of MCP.

## How it works

- **CLI (`etna`)** — manages the full lifecycle. Install kits, start the server, configure clients, manage Chrome for browser automation. One command for everything.
- **Kits (`~/.etna_server/kits/`)** — each kit is a plain Python file with `@tool`-decorated functions and four metadata fields. Drop one in, and it's live.
- **Server** — FastAPI process that auto-discovers all kits at startup and again on hot-reload. Speaks both the Etna Protocol and MCP JSON-RPC on the same port.
- **Etna Protocol** — a tree-based tool discovery protocol in the same category as MCP, not derived from it. Built directly on LLM tool use. The server is the root. Kits are branches. Tools are leaves. Clients walk the tree, toggle branches on and off per session, and search across tools by keyword. MCP compatibility is an adapter — it's how Etna connects to clients that already speak MCP, not the foundation the protocol is built on.
- **Skills** — markdown instruction documents paired with kits or installed standalone. A harness can discover and load skills into context, giving the model guidance on how to use a kit's tools or how to approach a class of tasks.
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

## .ekp packages

`.ekp` files bundle a kit and its skill documentation together in a single archive. The skill is installed alongside the kit and automatically surfaced to the model when it inspects that kit.

```
etna install my_package.ekp
```

An `.ekp` is a zip archive with this structure:

```
my_kit.py          ← the kit file (required)
skill/             ← skill folder (optional)
  SKILL.md         ← instructions for the model
  references/      ← reference docs, loaded on demand
  scripts/         ← helper scripts
  assets/          ← any other files
```

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
etna install my_package.ekp
```

If the server is already running, the kit is linted, installed, and hot-reloaded immediately — no restart needed.

To install from the curated kit repo:

```
etna install ntfy
etna install ntfy==1.0.0b1
```

## CLI reference

```
etna install                          First-time setup, or getting-started hints if ready
etna install <path/kit.py>            Install a kit from a local file
etna install <path/pkg.ekp>           Install a kit package (kit + skill)
etna install <kit_name>               Install a kit from the curated repo
etna install <kit_name>==<version>    Install a specific version from the repo
etna update <path/kit.py>            Update a kit (no prompt)
etna update <kit_name>               Update a kit from the repo (no prompt)
etna update --all                    Update all installed kits from the repo
etna remove <kit_name>               Remove a kit
etna list                            List installed kits
etna search <query>                  Search the curated kit repo
etna status                          Server, browser, kits, and client summary

etna start                           Start the server
etna start --verbose                 Start with full log output
etna start stdio [kit_name]          Start stdio shim — scopes to /mcp/<kit_name> if given
etna stop                            Stop the server
etna restart                         Restart the server

etna compat                          Auto-detect and configure all clients
etna compat claude                   Write kit entries to Claude Desktop config
etna compat lmstudio                 Write kit entries to LM Studio config
etna compat cursor                   Write kit entries to Cursor config
etna compat windsurf                 Write kit entries to Windsurf config
etna compat vscode                   Write kit entries to VS Code settings
etna compat continue                 Write kit entries to Continue config
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

| Endpoint             | Method | Description                                         |
|----------------------|--------|-----------------------------------------------------|
| `/list_kits`         | GET    | All installed kit names and descriptions            |
| `/inspect_kit`       | POST   | Kit metadata — name, description, skill             |
| `/list_tools_in_kit` | POST   | Full tool schemas for a kit                         |
| `/inspect_tool`      | POST   | Schema for a single tool                            |
| `/run_tool`          | POST   | Execute a tool, returns result directly             |
| `/reload_kit`        | POST   | Hot-reload a kit without restart                    |
| `/unload_kit`        | POST   | Remove a kit's tools from the registry              |
| `/search_tools`      | POST   | Keyword search across all tool names and docs       |
| `/list_skills`       | GET    | All installed general skills (name + description)   |
| `/read_skill`        | POST   | SKILL.md body for any skill — kit or general        |
| `/mcp`               | POST   | MCP JSON-RPC 2.0 — all kits                         |
| `/mcp`               | GET    | SSE keepalive stream — all kits                     |
| `/mcp/<kit_stem>`    | POST   | MCP JSON-RPC 2.0 — scoped to one kit                |
| `/mcp/<kit_stem>`    | GET    | SSE keepalive stream — scoped to one kit            |

### inspect_kit response

```json
{
  "kit_name": "Ntfy",
  "kit_description": "Send push notifications via ntfy.",
  "filename": "ntfy.py",
  "enabled": true,
  "skill": "ntfy"
}
```

`skill` is the skill name if a skill is installed for this kit, `null` otherwise. Use `read_skill` to retrieve the skill content.

### read_skill request/response

```json
{ "skill": "ntfy" }
```

```json
{ "name": "ntfy", "body": "..." }
```

Works for both kit skills (discovered via `inspect_kit`) and general skills (discovered via `list_skills`).

## Skills

Skills are markdown instruction documents that tell the model how to use a kit's tools or how to approach a class of tasks. They live in:

- `~/.etna_server/kit_skills/<kit_stem>/` — paired with a specific kit, surfaced via `inspect_kit`
- `~/.etna_server/skills/<skill_name>/` — standalone general skills, surfaced via `list_skills`

A skill folder contains at minimum a `SKILL.md` with YAML frontmatter:

```markdown
---
name: My Skill
description: What this skill teaches the model.
---

When using this kit, always...
```

Subdirectories (`references/`, `scripts/`, `assets/`) are optional and available for the model to traverse on demand.

## License

Apache 2.0
