"""
etna/cli.py — Etna command-line interface

Commands:
  etna install                               First-time setup: venv + OS service (if needed)
  etna install <path/to/kit.py|pkg.ekp|skill.skill>  Install from local file (autodetects type)
  etna install <name>                        Install kit or skill from repo (autodetects)
  etna install <name==version>               Install specific version from repo
  etna update <kit_name>                    Update an installed kit (no prompt)
  etna update --all                         Update all installed kits from the repo
  etna remove <kit_or_skill_name>           Remove a kit or skill (autodetects)

  etna kit list                             List installed kits
  etna kit update <kit_name>               Update an installed kit
  etna kit update --all                    Update all installed kits
  etna kit remove <kit_name>               Remove an installed kit
  etna kit inspect <kit_stem>              Show tools and details for an installed kit
  etna kit search <query>                  Search locally installed kits
  etna kit config list <kit>               List config variables for a kit
  etna kit config get <kit> <var>          Get a kit config value
  etna kit config set <kit> <var> <val>    Set a kit config value
  etna kit config reset <kit> <var>        Reset a kit config value to its default

  etna skill list                          List installed general skills
  etna skill remove <skill_stem>           Remove an installed skill
  etna skill inspect <skill_stem>          Show details for an installed skill
  etna skill search <query>               Search locally installed skills

  etna search <query>                      Search the curated repo — kits and skills
  etna search <query> --kit               Search repo kits only
  etna search <query> --skill             Search repo skills only
  etna search inspect <name>              Inspect a repo package without downloading
  etna list                               List installed kits and skills
  etna status                             Show server, browser, kits, and client status

  etna start                              Start the server
  etna start --verbose                    Start with full log output
  etna start stdio [kit_name]             Start stdio shim — scoped to kit if given
  etna stop                               Stop the server
  etna restart                            Restart the server

  etna compat                             Auto-detect and configure all clients
  etna compat claude                      Write kit entries to Claude Desktop config
  etna compat lmstudio                    Write kit entries to LM Studio config
  etna compat openwebui <url> <api_key>   Register kits with OpenWebUI

  etna config list <kit_name>             List config variables for a kit (legacy alias)
  etna config get <kit_name> <var>        Get a config value (legacy alias)
  etna config set <kit_name> <var> <val>  Set a config value (legacy alias)
  etna config reset <kit_name> <var>      Reset a config value to its default (legacy alias)

  etna browser start                      Launch Chrome with CDP
  etna browser stop                       Stop Chrome
  etna browser status                     Show Chrome CDP status
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from etna import config as cfg
from etna import kit_manager as km
from etna import server_manager as sm
from etna.server_manager import BASE_PORT
from etna.console import *  # noqa: F403

CDP_PORT = 9222


# ── Health check ──────────────────────────────────────────────────────────────

def _check_install() -> dict:
    return {
        "uv":      shutil.which("uv") is not None,
        "venv":    (cfg.VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") /
                    ("python.exe" if sys.platform == "win32" else "python")).exists(),
        "service": _service_registered(),
    }


def _service_registered() -> bool:
    if sys.platform.startswith("linux"):
        return (Path.home() / ".config" / "systemd" / "user" / "etna.service").exists()
    elif sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / "net.etna-mcp.etna.plist").exists()
    elif sys.platform == "win32":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "EtnaMCPServer"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return result.returncode == 0
    return False


def _show_hints():
    print(f"{PREFIX}{green}Etna is ready {green}✔{white}\n")
    print(f"  {white}Install a kit    {white}  {light_blue}etna {light_green}install {grey}<name or path/kit.py>{white}")
    print(f"  {white}Start the server {white}  {light_blue}etna {light_green}start{white}")
    print(f"  {white}List kits        {white}  {light_blue}etna {light_green}list{white}")
    print(f"  {white}Connect clients  {white}  {light_blue}etna {light_green}compat{white}")
    print()


# ── install / update ──────────────────────────────────────────────────────────

def cmd_install(args: list[str], is_update: bool = False):
    if not args and not is_update:
        checks = _check_install()
        if all(checks.values()):
            _show_hints()
            return
        steps = []
        if not checks["uv"]:
            print(f"{PREFIX}{red}UV not found. {bright_yellow}Install it from: {cyan}https://github.com/astral-sh/uv{white}")
            sys.exit(1)
        if not checks["venv"]:
            steps.append("venv")
        if not checks["service"]:
            steps.append("service")

        total = len(steps)
        for i, step in enumerate(steps):
            _, _, bar, _ = progress_bar(i, total, separate=True)
            spin = throbber(i)
            print(f"{clear_line}{spin} {bar}{white} ({grey}{i}{white}/{grey}{total}{white})", end="\n")
            print(f"{clear_line} {white}Setting up {light_blue}{step}{white}...", end="\r")
            if step == "venv":
                sm.ensure_venv()
            elif step == "service":
                sm.install_service()
            print(f"{up}{clear_line}", end="\r")

        # Collapse: move up past the 2 reserved lines, commit a single ✔ line
        print(f"{up}{up}{clear_line}{PREFIX}{green}Etna setup complete {green}✔{white}")
        print(f"{clear_line}", end="\r")
        return

    target = args[0]
    config = cfg.load()
    kits_dir = cfg.kits_dir()

    if target.endswith(".skill") or (Path(target).exists() and Path(target).is_dir() and (Path(target) / "SKILL.md").exists()):
        km.install_skill(target)
        return
    if target.endswith(".py") or target.endswith(".ekp") or "/" in target or "\\" in target or Path(target).exists():
        km.install_kit(target, config, kits_dir, is_update=is_update)
    else:
        # Autodetect: check repo manifest to see if it's a kit or skill
        import urllib.request as _req
        try:
            with _req.urlopen(km.REPO_MANIFEST_URL, timeout=8) as resp:
                manifest = json.loads(resp.read())
            bare_name = target.split("==")[0].strip().lower()
            is_skill = any(
                e.get("stem","").lower() == bare_name or e.get("name","").lower() == bare_name
                for e in manifest.get("skills", [])
            )
            if is_skill:
                km.install_skill_from_repo(target)
                return
        except Exception:
            pass
        km.install_kit_from_repo(target, config, kits_dir, is_update=is_update)

    cfg.save(config)

    # Sync all registered clients after install/update
    _sync_clients(config)


def cmd_update(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}update {light_grey}<kit_name or path/kit.py> {grey}[--all]{white}")
        sys.exit(1)

    if "--all" in args:
        config = cfg.load()
        kits_dir = cfg.kits_dir()
        kits = list(config.get("kits", {}).keys())
        if not kits:
            print(f"{PREFIX}{red}No kits installed.{white}")
            return
        total = len(kits)
        for i, kit_stem in enumerate(kits):
            print(f"{PREFIX}{grey}Updating kits {white}({grey}{i + 1}{white}/{grey}{total}{white}){white} — {light_grey}{kit_stem}{white}")
            km.install_kit_from_repo(kit_stem, config, kits_dir, is_update=True)
        cfg.save(config)
        _sync_clients(config)
    else:
        cmd_install(args, is_update=True)


def cmd_remove(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {red}remove {light_grey}<kit_or_skill_name>{white}")
        sys.exit(1)
    name = args[0]
    # Check if it's a skill first
    skill_dir = cfg.SKILLS_DIR / name if cfg.SKILLS_DIR.exists() else None
    is_skill = skill_dir and skill_dir.exists()
    if not is_skill and cfg.SKILLS_DIR.exists():
        for d in cfg.SKILLS_DIR.iterdir():
            if d.is_dir():
                meta = km.parse_skill_meta(d)
                if meta["name"].lower() == name.lower():
                    is_skill = True
                    break
    if is_skill:
        km.remove_skill(name)
        return
    config = cfg.load()
    kits_dir = cfg.kits_dir()
    km.remove_kit(name, config, kits_dir)
    cfg.save(config)
    _sync_clients(config)


# ── Client sync ───────────────────────────────────────────────────────────────

def _sync_clients(config: dict):
    """
    After any kit install/update/remove, re-sync all registered clients.
    Reports per-client success/warning/failure with appropriate colors.
    """
    clients = cfg.load_clients()
    if not clients:
        return

    print(f"\n{PREFIX}{grey}Syncing clients...{white}")

    import threading as _threading, time as _time
    for name, data in clients.items():
        display = {"claude": "Claude Desktop", "lmstudio": "LM Studio", "openwebui": "OpenWebUI"}.get(name, name)
        _done = [False]
        def _spin_client(d=display):
            tick = 0
            while not _done[0]:
                print(f"{clear_line}  {grey}{d}{white}: {yellow}{throbber(tick)}{white}", end="\r")
                tick += 1; _time.sleep(0.1)
        t = _threading.Thread(target=_spin_client, daemon=True); t.start()
        if name == "openwebui":
            _sync_openwebui(data, config)
        elif name in ("claude", "lmstudio"):
            _sync_stdio_client(name, data, config)
        else:
            _done[0] = True; t.join()
            print(f"{clear_line}  {grey}{name}{white}: {orange}unknown client type, skipped{white}")
            continue
        _done[0] = True; t.join()


_CLIENT_COLORED_NAMES = {
    "claude":    f"{orange}Claude Desktop{white}",
    "lmstudio":  f"{purple}LM Studio{white}",
    "cursor":    f"{grey}Cursor{white}",
    "windsurf":  f"{white}Windsurf{white}",
    "vscode":    f"{blue}VS Code{white}",
    "continue":  f"{grey}Continue{white}",
    "openwebui": f"{white}OpenWebUI{white}",
}


def _sync_stdio_client(name: str, data: dict, config: dict):
    """Re-sync a stdio-based client."""
    path = Path(data.get("path", ""))
    colored = _CLIENT_COLORED_NAMES.get(name, f"{grey}{name}{white}")
    servers_key = data.get("servers_key", "mcpServers")

    if not path.exists():
        print(f"  {colored}: {yellow}could not find JSON file {light_grey}{path}{white}")
        return

    try:
        if servers_key == "mcp.servers":
            _compat_write_vscode_config(path, colored, name, silent=True)
        elif servers_key == "modelContextProtocolServers":
            _compat_write_continue_config(path, colored, name, silent=True)
        else:
            _write_stdio_config(path, servers_key, config, silent=True)
        print(f"  {colored}: {light_green}✔ updated{white}")
    except Exception as e:
        print(f"  {colored}: {red}✘ failed — {light_grey}{e}{white}")


def _sync_openwebui(data: dict, config: dict):
    """Re-sync OpenWebUI via its API."""
    url  = data.get("url", "")
    key  = data.get("api_key", "")

    if not url or not key:
        print(f"  {grey}OpenWebUI{white}: {red}✘ missing url or api_key in clients.json{white}")
        return

    try:
        _do_openwebui_sync(url, key, config, silent=True)
        print(f"  {grey}OpenWebUI{white}: {green}✔ updated{white}")
    except _EndpointUnreachable:
        print(f"  {grey}OpenWebUI{white}: {orange}couldn't reach endpoint {light_grey}{url}{white}")
    except Exception as e:
        print(f"  {grey}OpenWebUI{white}: {red}✘ failed — {light_grey}{e}{white}")


class _EndpointUnreachable(Exception):
    pass


# ── list ──────────────────────────────────────────────────────────────────────

def cmd_list(args: list[str]):
    config = cfg.load()
    kits = config.get("kits", {})
    skills = km.list_installed_skills()

    # Kits
    if kits:
        print(f"{PREFIX}{grey}Kits{white}: {light_green}{len(kits)}{white}\n")
        for kit_stem, info in kits.items():
            display = info.get("kit_name", kit_stem)
            desc    = info.get("kit_description", "")
            print(f"  {grey}{display}{white} ({grey}{kit_stem}{white})")
            if desc:
                print(f"    {light_grey}{desc}{white}")
            print()
    else:
        print(f"{PREFIX}{grey}Kits{white}: {red}None installed{white}\n")

    # Skills
    if skills:
        print(f"{PREFIX}{grey}Skills{white}: {light_green}{len(skills)}{white}\n")
        for s in skills:
            print(f"  {grey}{s['name']}{white} ({grey}{s['stem']}{white})")
            if s.get("description"):
                print(f"    {light_grey}{s['description']}{white}")
            print()
    else:
        print(f"{PREFIX}{grey}Skills{white}: {red}None installed{white}\n")



# ── search ────────────────────────────────────────────────────────────────────

def cmd_search(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}search {grey}<query>{white}")
        sys.exit(1)

    query = " ".join(args).lower().strip()

    import threading as _threading, time as _time, urllib.request as _req, json as _json
    _done = [False]

    def _spin():
        tick = 0
        while not _done[0]:
            print(f"{clear_line}{PREFIX}{yellow}{throbber(tick)} Searching kit index{white}...", end="\r")
            tick += 1; _time.sleep(0.1)

    t = _threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        with _req.urlopen(km.REPO_MANIFEST_URL, timeout=10) as resp:
            manifest = _json.loads(resp.read())
        _done[0] = True; t.join()
    except Exception as e:
        _done[0] = True; t.join()
        print(f"{clear_line}{PREFIX}{red}Could not fetch kit index: {white}{light_grey}{e}{white}")
        sys.exit(1)

    keywords = query.split()
    results = []
    for entry in manifest.get("kits", []):
        name    = entry.get("name", "")
        stem    = entry.get("stem", "")
        desc    = entry.get("description", "")
        version = entry.get("version", "")
        searchable = f"{name} {stem} {desc}".lower()
        score = sum(1 for kw in keywords if kw in searchable)
        if score > 0:
            results.append((score, name, stem, desc, version))

    results.sort(key=lambda x: x[0], reverse=True)

    if not results:
        print(f"{clear_line}{PREFIX}{grey}No kits found matching {white}\'{grey}{query}{white}\'")
        return

    count = len(results)
    print(f"{clear_line}{PREFIX}{grey}Results{white}: {light_green}{count}{white}\n")

    config = cfg.load()
    installed = config.get("kits", {})

    for _, name, stem, desc, version in results:
        is_installed = stem in installed
        installed_tag = f" {green}✔{white}" if is_installed else ""
        print(f"  {grey}{name}{white} ({grey}{stem}{white}){installed_tag}  {light_grey}{version}{white}")
        if desc:
            print(f"    {light_grey}{desc}{white}")
        print()


# ── status ────────────────────────────────────────────────────────────────────

def cmd_status(args: list[str]):
    config = cfg.load()
    clients = cfg.load_clients()

    # Server
    port = config.get("port")
    if port and _port_open(port):
        server_str = f"{green}✔ Running{white}"
    else:
        server_str = f"{red}✘ Off{white}"

    # Browser
    chrome_installed = _find_chrome() is not None
    if not chrome_installed:
        browser_str = f"{yellow}✘ Not installed{white}"
    elif _cdp_alive():
        browser_str = f"{green}✔ Connected{white}"
    else:
        browser_str = f"{orange}✘ Not started{white}"

    # Kits
    kits = config.get("kits", {})
    kit_count = len(kits)
    if kit_count > 0:
        kits_str = f"{green}{kit_count} Installed{white}"
    else:
        kits_str = f"{red}None installed{white}"

    # Clients
    if clients:
        client_names = []
        for name in clients:
            if name == "claude":
                client_names.append("Claude Desktop")
            elif name == "lmstudio":
                client_names.append("LM Studio")
            elif name == "openwebui":
                client_names.append("Open WebUI")
            else:
                client_names.append(name)
        clients_str = f"{grey}{', '.join(client_names)}{white}"
    else:
        clients_str = f"{red}None configured{white}"

    # Print status card
    col = 10
    print(f"\n  {grey}Etna{white}\n")
    print(f"  {'Server'.ljust(col)}{server_str}")
    print(f"  {'Browser'.ljust(col)}{browser_str}")
    print(f"  {'Kits'.ljust(col)}{kits_str}")
    print(f"  {'Clients'.ljust(col)}{clients_str}")
    print()


# ── start / stop / restart ────────────────────────────────────────────────────

def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("localhost", port)) == 0


def cmd_start(args: list[str]):
    config = cfg.load()
    verbose = "--verbose" in args
    args = [a for a in args if a != "--verbose"]

    if args and args[0].lower() == "stdio":
        kit_name = args[1] if len(args) > 1 else None
        kit_stem = None

        if kit_name:
            if kit_name not in config.get("kits", {}):
                print(f"{PREFIX}{red}Unknown kit: {white}'{grey}{kit_name}{white}'")
                sys.exit(1)
            kit_stem = kit_name

        port = config.get("port")
        if not port or not _port_open(port):
            print(f"{PREFIX}{red}No server running. {bright_yellow}Start one with: {light_blue}etna{white} {light_green}start{white}", file=sys.stderr)
            sys.exit(1)

        from etna.stdio_shim import run_shim
        run_shim(port, kit_stem)
        return

    port = sm.start_server(config, verbose=verbose)
    config["port"] = port
    cfg.save(config)


def cmd_stop(args: list[str]):
    config = cfg.load()
    sm.stop_server(config)
    cfg.save(config)


def cmd_restart(args: list[str]):
    config = cfg.load()
    sm.stop_server(config)
    cfg.save(config)
    time.sleep(1)
    cmd_start([])


# ── compat ────────────────────────────────────────────────────────────────────

def cmd_compat(args: list[str]):
    if not args:
        _compat_auto()
        return

    target = args[0].lower()
    if target == "claude":
        path = _claude_config_path()
        _compat_write_stdio_config(path, "Claude Desktop", "claude", servers_key="mcpServers")
    elif target == "lmstudio":
        path = _lmstudio_config_path()
        _compat_write_stdio_config(path, "LM Studio", "lmstudio", servers_key="mcpServers")
    elif target == "openwebui":
        if len(args) < 3:
            print(f"Usage: {light_blue}etna{white} {grey}compat openwebui {light_grey}<url> <api_key>{white}")
            sys.exit(1)
        _compat_openwebui(args[1], args[2])
    else:
        print(f"{PREFIX}{red}Unknown compat target: {white}'{grey}{target}{white}'")
        print(f"{grey}Targets{white}: {grey}claude{white}, {grey}lmstudio{white}, {grey}openwebui{white}")
        sys.exit(1)


def _compat_auto():
    found = []

    claude_path = _claude_config_path()
    if claude_path.parent.exists():
        found.append(("claude", "Claude Desktop", claude_path, "mcpServers"))

    lmstudio_path = _lmstudio_config_path()
    if lmstudio_path.parent.exists():
        found.append(("lmstudio", "LM Studio", lmstudio_path, "mcpServers"))

    if not found:
        print(f"{PREFIX}{orange}No supported clients detected automatically.{white}")
        print(f"{PREFIX}{bright_yellow}Use: {light_blue}etna{white} {grey}compat claude{white} / {grey}lmstudio{white} / {grey}openwebui{white} {bright_yellow}to configure manually.{white}")
        return

    print(f"{PREFIX}{green}Detected:{white}")
    for _, name, path, _ in found:
        print(f"  {light_green}✔ {grey}{name}{white} ({light_grey}{path}{white})")

    answer = input(f"\n{PREFIX}{bright_yellow}Configure all? {white}[{light_green}y{white}/{red}n{white}]: ").strip().lower()
    if answer == "y":
        for target, name, path, servers_key in found:
            _compat_write_stdio_config(path, name, target, servers_key=servers_key)
    else:
        for target, name, path, servers_key in found:
            ans = input(f"{PREFIX}{bright_yellow}Configure {grey}{name}{bright_yellow}? {white}[{light_green}y{white}/{red}n{white}]: ").strip().lower()
            if ans == "y":
                _compat_write_stdio_config(path, name, target, servers_key=servers_key)
            else:
                print(f"{PREFIX}{grey}Skipped {white}(already registered){white}: {grey}{name}{white}")

    print(f"\n{PREFIX}{bright_yellow}Restart any configured clients for changes to take effect.{white}")


def _claude_config_path() -> Path:
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming" / "Claude"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        base = Path.home() / ".config" / "Claude"
    return base / "claude_desktop_config.json"


def _lmstudio_config_path() -> Path:
    return Path.home() / ".lmstudio" / "mcp.json"


def _cursor_config_path() -> Path:
    return Path.home() / ".cursor" / "mcp.json"


def _windsurf_config_path() -> Path:
    return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"


def _vscode_config_path() -> Path:
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming" / "Code" / "User"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Code" / "User"
    else:
        base = Path.home() / ".config" / "Code" / "User"
    return base / "settings.json"


def _continue_config_path() -> Path:
    return Path.home() / ".continue" / "config.json"


def _compat_write_stdio_config(config_path: Path, colored_name: str, client_key: str, servers_key: str = "mcpServers"):
    config = cfg.load()
    kits = config.get("kits", {})

    if not kits:
        print(f"{PREFIX}{red}No kits installed.{white}")
        sys.exit(1)

    _write_stdio_config(config_path, servers_key, config)

    cfg.register_client(client_key, {
        "path": str(config_path),
        "servers_key": servers_key,
    })

    print(f"{PREFIX}{light_green}Config updated: {white}{light_grey}{config_path}{white}")
    print(f"{PREFIX}{bright_yellow}Restart {colored_name}{bright_yellow} for changes to take effect.{white}")


def _compat_write_vscode_config(config_path: Path, colored_name: str, client_key: str, servers_key: str = None, silent: bool = False):
    """VS Code stores MCP servers under mcp.servers inside settings.json."""
    config = cfg.load()
    kits = config.get("kits", {})

    if not kits:
        print(f"{PREFIX}{red}No kits installed.{white}")
        sys.exit(1)

    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except Exception:
            if not silent:
                print(f"{PREFIX}{yellow}could not read {light_grey}{config_path}{white}")
            return

    mcp_section = existing.get("mcp", {})
    mcp_servers = mcp_section.get("servers", {})

    # Surgical edit — same logic as _write_stdio_config
    stale = [
        name for name, entry in mcp_servers.items()
        if isinstance(entry.get("args"), list)
        and len(entry["args"]) >= 3
        and entry["args"][0] == "start"
        and entry["args"][1] == "stdio"
        and entry["args"][2] not in kits
    ]
    for name in stale:
        del mcp_servers[name]
        if not silent:
            print(f"{PREFIX}{red}Removed stale: {grey}{name}{white}")

    already = {
        entry["args"][2]
        for entry in mcp_servers.values()
        if isinstance(entry.get("args"), list)
        and len(entry["args"]) >= 3
        and entry["args"][0] == "start"
        and entry["args"][1] == "stdio"
    }

    for kit_stem, info in kits.items():
        display_name = info.get("kit_name", kit_stem)
        if kit_stem not in already:
            mcp_servers[display_name] = {
                "command": "etna",
                "args": ["start", "stdio", kit_stem],
            }
            if not silent:
                print(f"{PREFIX}{light_green}Added: {grey}{display_name}{white}")

    mcp_section["servers"] = mcp_servers
    existing["mcp"] = mcp_section
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, config_path)

    cfg.register_client(client_key, {"path": str(config_path), "servers_key": "mcp.servers"})
    if not silent:
        print(f"{PREFIX}{light_green}Config updated: {white}{light_grey}{config_path}{white}")
        print(f"{PREFIX}{bright_yellow}Restart {colored_name}{bright_yellow} for changes to take effect.{white}")


def _compat_write_continue_config(config_path: Path, colored_name: str, client_key: str, servers_key: str = None, silent: bool = False):
    """Continue uses modelContextProtocolServers array with transport wrapper."""
    config = cfg.load()
    kits = config.get("kits", {})

    if not kits:
        print(f"{PREFIX}{red}No kits installed.{white}")
        sys.exit(1)

    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except Exception:
            if not silent:
                print(f"{PREFIX}{yellow}could not read {light_grey}{config_path}{white}")
            return

    servers = existing.get("modelContextProtocolServers", [])

    # Remove stale Etna entries — fingerprint: command == "etna", args[1] == "stdio"
    def _is_etna(s):
        t = s.get("transport", {})
        return t.get("command") == "etna" and len(t.get("args", [])) >= 2 and t["args"][0] == "start" and t["args"][1] == "stdio"

    def _stem(s):
        args = s.get("transport", {}).get("args", [])
        return args[2] if len(args) >= 3 else None

    stale = [s for s in servers if _is_etna(s) and _stem(s) not in kits]
    for s in stale:
        if not silent:
            print(f"{PREFIX}{red}Removed stale: {grey}{_stem(s)}{white}")
    servers = [s for s in servers if not (_is_etna(s) and _stem(s) not in kits)]

    existing_stems = {_stem(s) for s in servers if _is_etna(s)}

    for kit_stem, info in kits.items():
        if kit_stem not in existing_stems:
            display_name = info.get("kit_name", kit_stem)
            servers.append({
                "transport": {
                    "type": "stdio",
                    "command": "etna",
                    "args": ["start", "stdio", kit_stem],
                }
            })
            if not silent:
                print(f"{PREFIX}{light_green}Added: {grey}{display_name}{white}")

    existing["modelContextProtocolServers"] = servers
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, config_path)

    cfg.register_client(client_key, {"path": str(config_path), "servers_key": "modelContextProtocolServers"})
    if not silent:
        print(f"{PREFIX}{light_green}Config updated: {white}{light_grey}{config_path}{white}")
        print(f"{PREFIX}{bright_yellow}Restart {colored_name}{bright_yellow} for changes to take effect.{white}")


def _write_stdio_config(config_path: Path, servers_key: str, config: dict, silent: bool = False):
    """Core logic for writing stdio entries. Raises on failure."""
    kits = config.get("kits", {})

    existing = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                existing = json.load(f)
        except Exception as e:
            if not silent:
                print(f"{PREFIX}{yellow}could not find JSON file {white}{light_grey}{config_path}{white}")
            return  # Never overwrite a malformed or unreadable config

    mcp_servers = existing.get(servers_key, {})

    stale = [
        name for name, entry in mcp_servers.items()
        if isinstance(entry.get("args"), list)
        and len(entry["args"]) >= 3
        and entry["args"][0] == "start"
        and entry["args"][1] == "stdio"
        and entry["args"][2] not in kits
    ]
    for name in stale:
        del mcp_servers[name]
        if not silent:
            print(f"{PREFIX}{red}Removed stale entry: {white}{grey}{name}{white}")

    already_registered = {
        entry["args"][2]
        for entry in mcp_servers.values()
        if isinstance(entry.get("args"), list)
        and len(entry["args"]) >= 3
        and entry["args"][0] == "start"
        and entry["args"][1] == "stdio"
    }

    for kit_stem, info in kits.items():
        display_name = info.get("kit_name", kit_stem)
        if kit_stem in already_registered:
            if not silent:
                print(f"{PREFIX}{grey}Skipped {white}(already registered){white}: {grey}{display_name}{white}")
            continue
        mcp_servers[display_name] = {
            "command": "etna",
            "args": ["start", "stdio", kit_stem],
        }
        if not silent:
            print(f"{PREFIX}{light_green}Added: {grey}{display_name} {white}-> {light_blue}etna{white} {light_green}start stdio {grey}{kit_stem}{white}")

    existing[servers_key] = mcp_servers
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2)


def _compat_openwebui(base_url: str, api_key: str):
    config = cfg.load()
    kits = config.get("kits", {})

    if not kits:
        print(f"{PREFIX}{red}No kits installed.{white}")
        sys.exit(1)

    try:
        _do_openwebui_sync(base_url, api_key, config)
    except _EndpointUnreachable:
        print(f"{PREFIX}{orange}Couldn't reach endpoint {white}{light_grey}{base_url}{white}")
        return
    except Exception as e:
        print(f"{PREFIX}{red}Request failed: {white}{light_grey}{e}{white}")
        return

    # Register in clients.json
    cfg.register_client("openwebui", {
        "url": base_url.rstrip("/"),
        "api_key": api_key,
    })

    print(f"{PREFIX}{green}Tool servers updated successfully.{white}")
    print(f"{PREFIX}{yellow}You may need to refresh your OpenWebUI tab.{white}")


def _do_openwebui_sync(base_url: str, api_key: str, config: dict, silent: bool = False):
    """Core OpenWebUI sync logic. Raises _EndpointUnreachable or Exception on failure."""
    import urllib.request
    import urllib.error

    base_url = base_url.rstrip("/")
    kits = config.get("kits", {})
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    def _request(method: str, path: str, body=None) -> dict:
        url = f"{base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.URLError as e:
            raise _EndpointUnreachable(str(e))
        except urllib.error.HTTPError as e:
            raise Exception(f"HTTP {e.code}: {e.read().decode()}")

    if not silent:
        print(f"{PREFIX}{yellow}Fetching tool server connections from {cyan}{base_url}{white}...", end="\r")

    current = _request("GET", "/api/v1/configs/tool_servers")
    connections = current.get("TOOL_SERVER_CONNECTIONS", [])

    if not silent:
        print(f"{clear_line}", end="")

    existing_by_id = {
        c["info"]["id"]: i
        for i, c in enumerate(connections)
        if isinstance(c.get("info"), dict) and c["info"].get("id") in kits
    }

    for kit_stem, info in kits.items():
        display_name = info.get("kit_name", kit_stem)
        description  = info.get("kit_description", f"{display_name} Etna kit")

        entry = {
            "url": f"http://localhost:{BASE_PORT}/mcp/{kit_stem}",
            "path": "",
            "type": "mcp",
            "auth_type": "none",
            "headers": None,
            "key": "",
            "config": {"enable": True, "function_name_filter_list": "", "access_grants": []},
            "spec_type": "url",
            "spec": "",
            "info": {"id": kit_stem, "name": display_name, "description": description},
        }

        if kit_stem in existing_by_id:
            connections[existing_by_id[kit_stem]] = entry
            if not silent:
                print(f"{PREFIX}{green}Updated: {grey}{display_name} {white}-> {cyan}http://localhost:{BASE_PORT}/mcp/{kit_stem}{white}")
        else:
            connections.append(entry)
            if not silent:
                print(f"{PREFIX}{light_green}Added: {grey}{display_name} {white}-> {cyan}http://localhost:{BASE_PORT}/mcp/{kit_stem}{white}")

    connections = [
        c for c in connections
        if not (isinstance(c.get("info"), dict)
                and c["info"].get("id") not in kits
                and c["info"].get("id") in existing_by_id)
    ]

    if not silent:
        print(f"{PREFIX}{yellow}Saving tool server connections{white}...", end="\r")

    _request("POST", "/api/v1/configs/tool_servers", {"TOOL_SERVER_CONNECTIONS": connections})

    if not silent:
        print(f"{clear_line}", end="")


# ── config ────────────────────────────────────────────────────────────────────

def cmd_config(args: list[str]):
    if len(args) < 2:
        print(f"Usage: {light_blue}etna{white} {grey}config {light_grey}<get|set|list|reset> <kit_name> [variable] [value]{white}")
        sys.exit(1)

    subcommand = args[0].lower()
    kit_stem   = args[1]
    config     = cfg.load()

    if kit_stem not in config.get("kits", {}):
        print(f"{PREFIX}{red}Unknown kit: {white}'{grey}{kit_stem}{white}'")
        sys.exit(1)

    from etna.kit_manager import parse_kit_metadata
    kits_dir = cfg.kits_dir()
    kit_file = kits_dir / f"{kit_stem}.py"
    defaults = parse_kit_metadata(kit_file).get("config", {}) if kit_file.exists() else {}
    saved    = cfg.load_kit_config(kit_stem)

    if subcommand == "list":
        if not defaults:
            print(f"{PREFIX}{red}Kit {white}'{grey}{kit_stem}{white}' {red}has no config variables.{white}")
            return
        print(f"{white}Config for '{light_grey}{kit_stem}{white}':")
        for key, default in defaults.items():
            current = saved.get(key, default)
            marker  = f" {light_grey}(default){white}" if key not in saved else ""
            print(f"  {grey}{key}{white} = '{light_grey}{current}{white}'{marker}")

    elif subcommand == "get":
        if len(args) < 3:
            print(f"Usage: {light_blue}etna{white} {grey}config get {light_grey}<kit_name> <variable>{white}")
            sys.exit(1)
        var = args[2]
        if var not in defaults:
            print(f"{PREFIX}{red}Unknown config variable {white}{light_grey}{var} {red}for kit {white}'{grey}{kit_stem}{white}'")
            sys.exit(1)
        print(saved.get(var, defaults[var]))

    elif subcommand == "set":
        if len(args) < 4:
            print(f"Usage: {light_blue}etna{white} {grey}config set {light_grey}<kit_name> <variable> <value>{white}")
            sys.exit(1)
        var, value = args[2], args[3]
        if var not in defaults:
            print(f"{PREFIX}{red}Unknown config variable {white}{light_grey}{var} {red}for kit {white}'{grey}{kit_stem}{white}'")
            sys.exit(1)
        saved[var] = value
        cfg.save_kit_config(kit_stem, saved)
        print(f"{PREFIX}{green}Set {grey}{kit_stem}{white}.{light_grey}{var}{white} = {light_grey}{value!r}{white}")

    elif subcommand == "reset":
        if len(args) < 3:
            print(f"Usage: {light_blue}etna{white} {grey}config reset {light_grey}<kit_name> <variable>{white}")
            sys.exit(1)
        var = args[2]
        if var not in defaults:
            print(f"{PREFIX}{red}Unknown config variable {white}{light_grey}{var} {red}for kit {white}'{grey}{kit_stem}{white}'")
            sys.exit(1)
        saved.pop(var, None)
        cfg.save_kit_config(kit_stem, saved)
        print(f"{PREFIX}{green}Reset {grey}{kit_stem}{white}.{light_grey}{var}{white} to default ({light_grey}{defaults[var]!r}{white})")

    else:
        print(f"{PREFIX}{red}Unknown config subcommand: {white}'{grey}{subcommand}{white}'")
        sys.exit(1)


# ── browser ───────────────────────────────────────────────────────────────────

def cmd_browser(args: list[str]):
    subcommand = args[0].lower() if args else "status"
    if subcommand == "start":
        _browser_start()
    elif subcommand == "stop":
        _browser_stop()
    elif subcommand == "status":
        _browser_status()
    else:
        print(f"{PREFIX}{red}Unknown browser subcommand: {white}'{grey}{subcommand}{white}'")
        sys.exit(1)


def _cdp_alive() -> bool:
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://localhost:{CDP_PORT}/json/version", timeout=2) as r:
            data = json.loads(r.read())
            return bool(data.get("webSocketDebuggerUrl") or data.get("Browser"))
    except Exception:
        return False


def _find_chrome() -> str | None:
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
    elif sys.platform == "darwin":
        for c in [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]:
            if Path(c).exists():
                return c
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            path = shutil.which(name)
            if path:
                return path
    return None


def _browser_start():
    if _cdp_alive():
        print(f"{PREFIX}{green}Chrome CDP already running on port {grey}{CDP_PORT}{white}")
        return

    chrome = _find_chrome()
    if not chrome:
        print(f"{PREFIX}{red}Could not find Chrome. Install Chrome and try again.{white}")
        sys.exit(1)

    user_data_dir = str(cfg.CHROME_PROFILE)
    print(f"{PREFIX}{yellow}Launching Chrome with CDP on port {grey}{CDP_PORT}{white}...", end="\r")

    proc = subprocess.Popen(
        [chrome,
         f"--remote-debugging-port={CDP_PORT}",
         "--remote-allow-origins=*",
         f"--user-data-dir={user_data_dir}",
         "--no-first-run",
         "--no-default-browser-check"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cfg.CHROME_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg.CHROME_PID_FILE.write_text(str(proc.pid))

    for i in range(40):
        time.sleep(0.5)
        spin = throbber(i)
        print(f"{clear_line}{PREFIX}{yellow}{spin} Waiting for Chrome on port {grey}{CDP_PORT}{white}...", end="\r")
        if _cdp_alive():
            print(f"{clear_line}{PREFIX}{green}Chrome ready on port {grey}{CDP_PORT}{white}")
            return

    print(f"{clear_line}{PREFIX}{orange}Warning: Chrome did not become ready in time.{white}", file=sys.stderr)


def _browser_stop():
    pid_file = cfg.CHROME_PID_FILE
    if not pid_file.exists():
        print(f"{PREFIX}{red}Chrome{white}: no PID file found.{white}")
        return
    try:
        pid = int(pid_file.read_text().strip())
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        print(f"{PREFIX}{green}Chrome stopped.{white}")
    except Exception as e:
        print(f"{PREFIX}{red}Could not stop Chrome: {white}{light_grey}{e}{white}")


def _browser_status():
    if _cdp_alive():
        print(f"{PREFIX}{grey}Chrome CDP{white}: {green}running on port {grey}{CDP_PORT}{white}")
    else:
        print(f"{PREFIX}{grey}Chrome CDP{white}: {red}NOT running{white}")
        print(f"{PREFIX}{bright_yellow}Run: {light_blue}etna{white} {light_green}browser start {grey}to launch Chrome{white}")


# ── Entry point ───────────────────────────────────────────────────────────────

def _print_help():
    """Print the CLI help with full color formatting."""
    import re as _re

    LB = light_blue   # etna
    G  = light_green  # constructive verbs
    R  = red          # destructive verbs
    Y  = yellow       # compat
    O  = orange       # compat subcommands, browser
    Pu = purple       # lmstudio, config
    C  = cyan         # stdio
    W  = white        # neutral / reset
    gr = grey         # <variables>, section headers

    e = f"{LB}etna{W}"

    def row(cmd, desc):
        plain = _re.sub(r'\033\[[^m]*m', '', cmd)
        pad = max(0, 44 - len(plain))
        print(f"{cmd}{' ' * pad}{W}{desc}{W}")

    def section(name):
        print(f"\n{gr}{name}{W}")

    print(f"\n{PREFIX}{gr}Etna command-line interface{W}\n")

    section("General")
    row(f"{e} {W}list{W}",                                           "List installed kits and skills")
    row(f"{e} {W}status{W}",                                         "Server, browser, kits, and client summary")

    section("Install & update")
    row(f"{e} {G}install{W}",                                        "First-time setup, or getting-started hints if ready")
    row(f"{e} {G}install{W} {gr}<path/kit.py|pkg.ekp|skill.skill>{W}", "Install from a local file (autodetects type)")
    row(f"{e} {G}install{W} {gr}<name>{W}",                          "Install kit or skill from the repo (autodetects)")
    row(f"{e} {G}install{W} {gr}<name==version>{W}",                 "Install a specific version from the repo")
    row(f"{e} {G}update{W} {gr}<kit_name>{W}",                       "Update an installed kit (no prompt)")
    row(f"{e} {G}update{W} {gr}--all{W}",                            "Update all installed kits from the repo")
    row(f"{e} {R}remove{W} {gr}<kit_or_skill_name>{W}",              "Remove a kit or skill (autodetects)")

    section("Kits")
    row(f"{e} {G}kit{W} {W}list{W}",                                 "List installed kits")
    row(f"{e} {G}kit{W} {G}update{W} {gr}<kit_name>{W}",             "Update an installed kit")
    row(f"{e} {G}kit{W} {G}update{W} {gr}--all{W}",                  "Update all installed kits")
    row(f"{e} {G}kit{W} {R}remove{W} {gr}<kit_name>{W}",             "Remove an installed kit")
    row(f"{e} {G}kit{W} {W}inspect{W} {gr}<kit_stem>{W}",            "Show tools and details for an installed kit")
    row(f"{e} {G}kit{W} {W}search{W} {gr}<query>{W}",                "Search locally installed kits")
    row(f"{e} {G}kit{W} {Pu}config{W} {W}list{W} {gr}<kit>{W}",      "List config variables for a kit")
    row(f"{e} {G}kit{W} {Pu}config{W} {W}get{W} {gr}<kit> <var>{W}", "Get a kit config value")
    row(f"{e} {G}kit{W} {Pu}config{W} {G}set{W} {gr}<kit> <var> <val>{W}", "Set a kit config value")
    row(f"{e} {G}kit{W} {Pu}config{W} {R}reset{W} {gr}<kit> <var>{W}", "Reset a kit config value to default")

    section("Skills")
    row(f"{e} {G}skill{W} {W}list{W}",                               "List installed general skills")
    row(f"{e} {G}skill{W} {R}remove{W} {gr}<skill_stem>{W}",         "Remove an installed skill")
    row(f"{e} {G}skill{W} {W}inspect{W} {gr}<skill_stem>{W}",        "Show details for an installed skill")
    row(f"{e} {G}skill{W} {W}search{W} {gr}<query>{W}",              "Search locally installed skills")

    section("Repo search")
    row(f"{e} {G}search{W} {gr}<query>{W}",                          "Search the curated repo — kits and skills")
    row(f"{e} {G}search{W} {gr}<query> --kit{W}",                    "Search repo kits only")
    row(f"{e} {G}search{W} {gr}<query> --skill{W}",                  "Search repo skills only")
    row(f"{e} {G}search{W} {W}inspect{W} {gr}<name>{W}",             "Inspect a repo package without downloading")

    section("Server")
    row(f"{e} {G}start{W}",                                          "Start the server")
    row(f"{e} {G}start{W} {gr}--verbose{W}",                         "Start with full log output")
    row(f"{e} {G}start{W} {C}stdio{W} {gr}[kit_name]{W}",           "Start stdio shim — scoped to kit if given")
    row(f"{e} {R}stop{W}",                                           "Stop the server")
    row(f"{e} {G}restart{W}",                                        "Restart the server")

    section("Client compat")
    row(f"{e} {Y}compat{W}",                                         "Auto-detect and configure all clients")
    row(f"{e} {Y}compat{W} {O}claude{W}",                            "Write kit entries to Claude Desktop config")
    row(f"{e} {Y}compat{W} {Pu}lmstudio{W}",                         "Write kit entries to LM Studio config")
    row(f"{e} {Y}compat{W} {gr}cursor{W}",                           "Write kit entries to Cursor config")
    row(f"{e} {Y}compat{W} {W}windsurf{W}",                          "Write kit entries to Windsurf config")
    row(f"{e} {Y}compat{W} {blue}vscode{W}",                         "Write kit entries to VS Code settings")
    row(f"{e} {Y}compat{W} {gr}continue{W}",                         "Write kit entries to Continue config")
    row(f"{e} {Y}compat{W} {W}openwebui{W} {gr}<url> <key>{W}",     "Register kits with OpenWebUI")

    section("Browser")
    row(f"{e} {O}browser{W} {G}start{W}",                            "Launch Chrome with CDP")
    row(f"{e} {O}browser{W} {R}stop{W}",                             "Stop Chrome")
    row(f"{e} {O}browser{W} {W}status{W}",                           "Show Chrome CDP status")

    print()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = sys.argv[1:]

    if not args:
        _print_help()
        sys.exit(0)

    if args[0] in ("--version", "-v"):
        from etna import __version__
        print(f"{light_blue}etna-mcp{white} {grey}{__version__}{white}")
        sys.exit(0)

    command = args[0].lower()
    rest    = args[1:]

    if command == "install":
        cmd_install(rest)
    elif command == "update":
        cmd_update(rest)
    elif command == "remove":
        cmd_remove(rest)
    elif command == "list":
        cmd_list(rest)
    elif command == "search":
        cmd_search(rest)
    elif command == "kit":
        cmd_kit(rest)
    elif command == "skill":
        cmd_skill(rest)
    elif command == "status":
        cmd_status(rest)
    elif command == "start":
        cmd_start(rest)
    elif command == "stop":
        cmd_stop(rest)
    elif command == "restart":
        cmd_restart(rest)
    elif command == "compat":
        cmd_compat(rest)
    elif command == "config":
        cmd_config(rest)
    elif command == "browser":
        cmd_browser(rest)
    else:
        print(f"{PREFIX}{red}Unknown command: {white}'{grey}{command}{white}'")
        print(f"{grey}Commands{white}: install, update, remove, kit, skill, search, list, status, start, stop, restart, compat, config, browser")
        sys.exit(1)


# ── kit (subcommand group) ────────────────────────────────────────────────────

def cmd_kit(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}kit{white} {grey}<list|update|remove|inspect|search|config>{white}")
        sys.exit(1)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "list":
        cmd_list([])
    elif sub == "update":
        cmd_update(rest)
    elif sub == "remove":
        cmd_remove(rest)
    elif sub == "inspect":
        cmd_kit_inspect(rest)
    elif sub == "search":
        cmd_kit_search(rest)
    elif sub == "config":
        cmd_config(rest)
    else:
        print(f"{PREFIX}{red}Unknown kit subcommand: {white}'{grey}{sub}{white}'")
        sys.exit(1)


def cmd_kit_inspect(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}kit{white} {white}inspect {grey}<kit_stem>{white}")
        sys.exit(1)

    kit_stem = args[0]
    config = cfg.load()
    kits_dir = cfg.kits_dir()

    if kit_stem not in config.get("kits", {}):
        print(f"{PREFIX}{red}Kit not installed: {white}'{grey}{kit_stem}{white}'")
        sys.exit(1)

    kit_file = kits_dir / f"{kit_stem}.py"
    from etna.kit_manager import parse_kit_metadata
    meta = parse_kit_metadata(kit_file) if kit_file.exists() else {}

    kit_info = config["kits"][kit_stem]
    name = kit_info.get("kit_name", kit_stem)
    desc = kit_info.get("kit_description", "")

    # Check for attached skill
    skill_dir = cfg.kit_skill_path(kit_stem)
    has_skill = skill_dir.exists() and (skill_dir / "SKILL.md").exists()

    print(f"\n{grey}{name}{white} ({grey}{kit_stem}{white})")
    if desc:
        print(f"  {light_grey}{desc}{white}")
    if has_skill:
        print(f"  {green}Skill attached{white}")
    print()

    # Config section
    defaults = meta.get("config", {})
    if defaults:
        saved = cfg.load_kit_config(kit_stem)
        print(f"  {grey}Config{white}\n")
        for key, default in defaults.items():
            current = saved.get(key, default)
            marker = f"  {grey}(default){white}" if key not in saved else f"  {green}(set){white}"
            print(f"    {grey}{key}{white}  {light_grey}{current}{white}{marker}")
        print()

    # Tools section
    if kit_file.exists():
        import ast as _ast
        try:
            tree = _ast.parse(kit_file.read_text(encoding="utf-8"))
        except Exception:
            tree = None

        if tree:
            tools_found = False
            for node in _ast.walk(tree):
                if not isinstance(node, _ast.FunctionDef):
                    continue
                for dec in node.decorator_list:
                    if (isinstance(dec, _ast.Name) and dec.id == "tool") or \
                       (isinstance(dec, _ast.Attribute) and dec.attr == "tool"):
                        if not tools_found:
                            print(f"  {grey}Tools{white}\n")
                            tools_found = True
                        doc = (_ast.get_docstring(node) or "").strip()
                        first_line = doc.split("\n")[0] if doc else ""
                        print(f"    {grey}{node.name}{white}")
                        if first_line:
                            print(f"      {white}{first_line}{white}")
                        print()
                        break


def cmd_kit_search(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}kit{white} {white}search {grey}<query>{white}")
        sys.exit(1)

    query = " ".join(args).lower()
    config = cfg.load()
    kits = config.get("kits", {})

    if not kits:
        print(f"{PREFIX}{red}No kits installed.{white}")
        return

    keywords = query.split()
    results = []
    for stem, info in kits.items():
        name = info.get("kit_name", stem)
        desc = info.get("kit_description", "")
        searchable = f"{name} {stem} {desc}".lower()
        score = sum(1 for kw in keywords if kw in searchable)
        if score > 0:
            results.append((score, name, stem, desc))

    if not results:
        print(f"{PREFIX}{grey}No installed kits matching {white}'{grey}{query}{white}'")
        return

    results.sort(key=lambda x: x[0], reverse=True)
    print(f"{PREFIX}{grey}Local results{white}: {light_green}{len(results)}{white}\n")
    for _, name, stem, desc in results:
        print(f"  {grey}{name}{white} ({grey}{stem}{white})")
        if desc:
            print(f"    {light_grey}{desc}{white}")
        print()


# ── skill (subcommand group) ──────────────────────────────────────────────────

def cmd_skill(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}skill{white} {grey}<list|remove|inspect|search>{white}")
        sys.exit(1)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "list":
        cmd_skill_list()
    elif sub == "remove":
        cmd_skill_remove(rest)
    elif sub == "inspect":
        cmd_skill_inspect(rest)
    elif sub == "search":
        cmd_skill_search(rest)
    else:
        print(f"{PREFIX}{red}Unknown skill subcommand: {white}'{grey}{sub}{white}'")
        sys.exit(1)


def cmd_skill_list():
    skills = km.list_installed_skills()
    if not skills:
        print(f"{PREFIX}{grey}Skills{white}: {red}None installed{white}\n")
        return
    print(f"{PREFIX}{grey}Skills{white}: {light_green}{len(skills)}{white}\n")
    for s in skills:
        print(f"  {grey}{s['name']}{white} ({grey}{s['stem']}{white})")
        if s.get("description"):
            print(f"    {light_grey}{s['description']}{white}")
        print()


def cmd_skill_remove(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {red}skill{white} {red}remove {grey}<skill_stem>{white}")
        sys.exit(1)
    km.remove_skill(args[0])


def cmd_skill_inspect(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}skill{white} {white}inspect {grey}<skill_stem>{white}")
        sys.exit(1)

    stem = args[0]
    skill_dir = cfg.skills_dir() / stem
    if not skill_dir.exists():
        # Try by name
        for d in cfg.SKILLS_DIR.iterdir():
            if d.is_dir():
                meta = km.parse_skill_meta(d)
                if meta["name"].lower() == stem.lower():
                    skill_dir = d
                    stem = d.name
                    break
        else:
            print(f"{PREFIX}{red}Skill not found: {white}'{grey}{stem}{white}'")
            sys.exit(1)

    meta = km.parse_skill_meta(skill_dir)
    print(f"\n  {grey}{meta['name']}{white} ({grey}{stem}{white})")
    if meta.get("description"):
        print(f"    {light_grey}{meta['description']}{white}")
    print()

    # List contents
    print(f"  {grey}Contents{white}")
    for item in sorted(skill_dir.rglob("*")):
        rel = item.relative_to(skill_dir)
        indent = "    " + "  " * (len(rel.parts) - 1)
        if item.is_dir():
            print(f"{indent}{grey}{rel.name}/{white}")
        else:
            print(f"{indent}{light_grey}{rel.name}{white}")
    print()


def cmd_skill_search(args: list[str]):
    if not args:
        print(f"Usage: {light_blue}etna{white} {light_green}skill{white} {white}search {grey}<query>{white}")
        sys.exit(1)

    query = " ".join(args).lower()
    skills = km.list_installed_skills()

    if not skills:
        print(f"{PREFIX}{red}No skills installed.{white}")
        return

    keywords = query.split()
    results = []
    for s in skills:
        searchable = f"{s['name']} {s['stem']} {s.get('description', '')}".lower()
        score = sum(1 for kw in keywords if kw in searchable)
        if score > 0:
            results.append((score, s))

    if not results:
        print(f"{PREFIX}{grey}No installed skills matching {white}'{grey}{query}{white}'")
        return

    results.sort(key=lambda x: x[0], reverse=True)
    print(f"{PREFIX}{grey}Local results{white}: {light_green}{len(results)}{white}\n")
    for _, s in results:
        print(f"  {grey}{s['name']}{white} ({grey}{s['stem']}{white})")
        if s.get("description"):
            print(f"    {light_grey}{s['description']}{white}")
        print()


# ── search (repo, with --kit/--skill flags and inspect subcommand) ────────────

def cmd_search(args: list[str]):
    import threading as _threading, time as _time, urllib.request as _req

    # Handle: etna search inspect <name>
    if args and args[0].lower() == "inspect":
        if len(args) < 2:
            print(f"Usage: {light_blue}etna{white} {light_green}search{white} {white}inspect {grey}<name>{white}")
            sys.exit(1)
        _cmd_search_inspect(args[1])
        return

    # Parse --kit / --skill flags
    kit_only   = "--kit" in args
    skill_only = "--skill" in args
    query_parts = [a for a in args if not a.startswith("--")]

    if not query_parts:
        print(f"Usage: {light_blue}etna{white} {light_green}search{white} {grey}<query> [--kit|--skill]{white}")
        sys.exit(1)

    query = " ".join(query_parts).lower()
    keywords = query.split()

    _done = [False]
    def _spin():
        tick = 0
        while not _done[0]:
            print(f"{clear_line}{PREFIX}{yellow}{throbber(tick)} Searching repo{white}...", end="\r")
            tick += 1; _time.sleep(0.1)

    t = _threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        with _req.urlopen(km.REPO_MANIFEST_URL, timeout=10) as resp:
            manifest = json.loads(resp.read())
        _done[0] = True; t.join()
    except Exception as e:
        _done[0] = True; t.join()
        print(f"{clear_line}{PREFIX}{red}Could not fetch repo index: {white}{light_grey}{e}{white}")
        sys.exit(1)

    config = cfg.load()
    installed_kits   = set(config.get("kits", {}).keys())
    installed_skills = {d.name for d in cfg.SKILLS_DIR.iterdir() if d.is_dir()} if cfg.SKILLS_DIR.exists() else set()

    kit_results   = []
    skill_results = []

    if not skill_only:
        for entry in manifest.get("kits", []):
            searchable = f"{entry.get('name','')} {entry.get('stem','')} {entry.get('description','')}".lower()
            score = sum(1 for kw in keywords if kw in searchable)
            if score > 0:
                kit_results.append((score, entry))
        kit_results.sort(key=lambda x: x[0], reverse=True)

    if not kit_only:
        for entry in manifest.get("skills", []):
            searchable = f"{entry.get('name','')} {entry.get('stem','')} {entry.get('description','')}".lower()
            score = sum(1 for kw in keywords if kw in searchable)
            if score > 0:
                skill_results.append((score, entry))
        skill_results.sort(key=lambda x: x[0], reverse=True)

    total = len(kit_results) + len(skill_results)
    if total == 0:
        print(f"{clear_line}{PREFIX}{grey}No results for {white}'{grey}{query}{white}'")
        return

    print(f"{clear_line}{PREFIX}{grey}Results{white}: {light_green}{total}{white}\n")

    if kit_results:
        if skill_results:
            print(f"  {grey}Kits{white}\n")
        for _, entry in kit_results:
            stem    = entry.get("stem", "")
            name    = entry.get("name", "")
            version = entry.get("version", "")
            desc    = entry.get("description", "")
            tag     = f" {green}✔{white}" if stem in installed_kits else ""
            print(f"  {grey}{name}{white} ({grey}{stem}{white}){tag}  {light_grey}{version}{white}")
            if desc:
                print(f"    {light_grey}{desc}{white}")
            print()

    if skill_results:
        if kit_results:
            print(f"  {grey}Skills{white}\n")
        for _, entry in skill_results:
            stem    = entry.get("stem", "")
            name    = entry.get("name", "")
            version = entry.get("version", "")
            desc    = entry.get("description", "")
            tag     = f" {green}✔{white}" if stem in installed_skills else ""
            print(f"  {grey}{name}{white} ({grey}{stem}{white}){tag}  {light_grey}{version}{white}")
            if desc:
                print(f"    {light_grey}{desc}{white}")
            print()


def _cmd_search_inspect(name: str):
    result = km.repo_inspect(name)
    if result is None:
        print(f"{clear_line}{PREFIX}{red}'{grey}{name}{white}' {red}not found in repo.{white}")
        sys.exit(1)

    print(f"\n  {grey}{result['name']}{white} ({grey}{result['stem']}{white})  {light_grey}{result['version']}{white}\n")
    if result.get("description"):
        print(f"    {light_grey}{result['description']}{white}\n")

    if result["type"] == "kit":
        tools = result.get("tools", [])
        if tools:
            print(f"  {grey}Tools{white}\n")
            for tool in tools:
                print(f"  {light_grey}{tool['name']}{white}")
                if tool.get("description"):
                    print(f"    {grey}{tool['description']}{white}")
                print()
        skill_str = f"{green}✔{white}" if result.get("has_skill") else f"{grey}—{white}"
        print(f"  {grey}Skill{white}  {skill_str}\n")

    elif result["type"] == "skill":
        contents = result.get("contents", [])
        if contents:
            print(f"  {grey}Contents{white}")
            for item in contents:
                print(f"    {light_grey}{item}{white}")
        print()
