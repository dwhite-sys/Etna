"""
etna/cli.py — Etna command-line interface

Commands:
  etna install                               First-time setup: venv + OS service (if needed)
                                             Shows getting-started hints if already installed
  etna install <path/to/kit.py>              Install a kit from a local file
  etna install <kit_name>                    Install a kit from the curated repo
  etna update <path/to/kit.py>              Update a kit from a local file (no prompt)
  etna update <kit_name>                    Update a kit from the curated repo (no prompt)
  etna update --all                         Update all installed kits from the repo
  etna remove <kit_name>                    Remove a kit
  etna list                                 List installed kits and server status
  etna status                               Show server, browser, kits, and client status

  etna start                                Start the server
  etna start --verbose                      Start with full log output
  etna start stdio                          Start stdio shim (all kits)
  etna start stdio <kit_name>               Start stdio shim (single kit)
  etna stop                                 Stop the server
  etna restart                              Restart the server

  etna compat                               Auto-detect and configure all clients
  etna compat claude                        Write kit entries to Claude Desktop config
  etna compat lmstudio                      Write kit entries to LM Studio config
  etna compat openwebui <url> <api_key>     Register kits with OpenWebUI

  etna config list <kit_name>               List config variables for a kit
  etna config get <kit_name> <var>          Get a config value
  etna config set <kit_name> <var> <val>    Set a config value
  etna config reset <kit_name> <var>        Reset a config value to its default

  etna browser start                        Launch Chrome with CDP
  etna browser stop                         Stop Chrome
  etna browser status                       Show Chrome CDP status
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
from etna.console import *

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

    if target.endswith(".py") or "/" in target or "\\" in target or Path(target).exists():
        km.install_kit(target, config, kits_dir, is_update=is_update)
    else:
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
        print(f"Usage: {light_blue}etna{white} {red}remove {light_grey}<kit_name>{white}")
        sys.exit(1)
    config = cfg.load()
    kits_dir = cfg.kits_dir()
    km.remove_kit(args[0], config, kits_dir)
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


def _sync_stdio_client(name: str, data: dict, config: dict):
    """Re-sync a stdio-based client (Claude Desktop, LM Studio)."""
    path = Path(data.get("path", ""))
    display = "Claude Desktop" if name == "claude" else "LM Studio"

    if not path.exists():
        print(f"  {grey}{display}{white}: {yellow}could not find JSON file {light_grey}{path}{white}")
        return

    try:
        servers_key = data.get("servers_key", "mcpServers")
        _write_stdio_config(path, servers_key, config, silent=True)
        print(f"  {grey}{display}{white}: {green}✔ updated{white}")
    except Exception as e:
        print(f"  {grey}{display}{white}: {red}✘ failed — {light_grey}{e}{white}")


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
    port = config.get("port")

    if port and _port_open(port):
        print(f"{PREFIX}{grey}Server{white}: {green}http://localhost:{port}{white}\n")
    else:
        print(f"{PREFIX}{grey}Server{white}: {red}OFF{white}\n")

    kits = config.get("kits", {})
    if not kits:
        print(f"{PREFIX}{red}No kits installed.{white}")
        return

    for kit_stem, info in kits.items():
        display = info.get("kit_name", kit_stem)
        desc    = info.get("kit_description", "")
        enabled = info.get("enabled", True)
        status_color = green if enabled else red
        status_label = "enabled" if enabled else "disabled"
        print(f"{white}[{status_color}{status_label}{white}] {grey}{display}{white} ({grey}{kit_stem}{white})")
        if desc:
            print(f"  {light_grey}{desc}{white}")


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


def _compat_write_stdio_config(config_path: Path, app_name: str, client_key: str, servers_key: str = "mcpServers"):
    config = cfg.load()
    kits = config.get("kits", {})

    if not kits:
        print(f"{PREFIX}{red}No kits installed.{white}")
        sys.exit(1)

    _write_stdio_config(config_path, servers_key, config)

    # Register in clients.json
    cfg.register_client(client_key, {
        "path": str(config_path),
        "servers_key": servers_key,
    })

    print(f"{PREFIX}{green}Config updated: {white}{light_grey}{config_path}{white}")
    print(f"{PREFIX}{bright_yellow}Restart {grey}{app_name} {bright_yellow}for changes to take effect.{white}")


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

    section("Kit management")
    row(f"{e} {G}install{W}",                                        "First-time setup, or getting-started hints if ready")
    row(f"{e} {G}install{W} {gr}<path/kit.py>{W}",                   "Install a kit from a local file")
    row(f"{e} {G}install{W} {gr}<kit_name>{W}",                      "Install a kit from the curated repo")
    row(f"{e} {G}update{W} {gr}<path/kit.py>{W}",                    "Update a kit (no prompt)")
    row(f"{e} {G}update{W} {gr}<kit_name>{W}",                       "Update a kit from the repo (no prompt)")
    row(f"{e} {G}update{W} {gr}--all{W}",                            "Update all installed kits from the repo")
    row(f"{e} {R}remove{W} {gr}<kit_name>{W}",                       "Remove a kit")
    row(f"{e} {G}list{W}",                                           "List installed kits and server status")
    row(f"{e} {G}status{W}",                                         "Server, browser, kits, and client summary")

    section("Server")
    row(f"{e} {G}start{W}",                                          "Start the server")
    row(f"{e} {G}start{W} {gr}--verbose{W}",                         "Start with full log output")
    row(f"{e} {G}start{W} {C}stdio{W} {gr}[kit_name]{W}",           "Start stdio shim — scoped to kit if given")
    row(f"{e} {G}stop{W}",                                           "Stop the server")
    row(f"{e} {G}restart{W}",                                        "Restart the server")

    section("Client compat")
    row(f"{e} {Y}compat{W}",                                         "Auto-detect and configure all clients")
    row(f"{e} {Y}compat{W} {O}claude{W}",                            "Write kit entries to Claude Desktop config")
    row(f"{e} {Y}compat{W} {Pu}lmstudio{W}",                         "Write kit entries to LM Studio config")
    row(f"{e} {Y}compat{W} {W}openwebui{W} {gr}<url> <key>{W}",     "Register kits with OpenWebUI")

    section("Kit config")
    row(f"{e} {Pu}config{W} {W}list{W} {gr}<kit>{W}",               "List config variables for a kit")
    row(f"{e} {Pu}config{W} {G}get{W} {gr}<kit> <var>{W}",          "Get a config value")
    row(f"{e} {Pu}config{W} {G}set{W} {gr}<kit> <var> <val>{W}",    "Set a config value")
    row(f"{e} {Pu}config{W} {R}reset{W} {gr}<kit> <var>{W}",        "Reset a config value to its default")

    section("Browser")
    row(f"{e} {O}browser{W} {G}start{W}",                            "Launch Chrome with CDP")
    row(f"{e} {O}browser{W} {R}stop{W}",                             "Stop Chrome")
    row(f"{e} {O}browser{W} {G}status{W}",                           "Show Chrome CDP status")

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
        print(f"{grey}Commands{white}: install, update, remove, list, status, start, stop, restart, compat, config, browser")
        sys.exit(1)
