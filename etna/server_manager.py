"""
etna/server_manager.py — Server lifecycle management

Handles:
  - UV venv self-repair (create if missing, install core deps)
  - Starting/stopping the main FastAPI server process
  - OS service registration (systemd / launchd / Task Scheduler)

Per-kit MCP scoping is handled by the server itself via /mcp/<kit_stem> URLs.
No per-kit processes or port assignments needed.
"""

import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from etna import config as cfg
from etna.console import *

BASE_PORT = 8467


# ── Venv self-repair ──────────────────────────────────────────────────────────

CORE_DEPS = ["fastapi", "uvicorn[standard]", "requests"]


def ensure_venv():
    """
    Create the Etna venv if it doesn't exist, then install core dependencies.
    Uses UV for both operations. Skips dep install if uvicorn is already present.
    """
    venv = cfg.VENV_DIR

    if not shutil.which("uv"):
        print(f"{PREFIX}{red}UV not found. {bright_yellow}Install it from: {cyan}https://github.com/astral-sh/uv{white}")
        sys.exit(1)

    # If venv exists and uvicorn is already installed, skip dep install but always
    # write the utils shim and ensure the kits dir exists
    uvicorn = _uvicorn_bin()
    if venv.exists() and uvicorn and uvicorn.exists():
        _write_utils_shim()
        cfg.kits_dir()
        return

    if not venv.exists():
        import threading as _threading
        _done = [False]
        def _spin_venv():
            tick = 0
            while not _done[0]:
                spin = throbber(tick)
                print(f"{clear_line}{PREFIX}{yellow}{spin} Creating venv at {grey}{venv}{white}...", end="\r")
                tick += 1
                import time as _t; _t.sleep(0.1)
        t = _threading.Thread(target=_spin_venv, daemon=True)
        t.start()
        try:
            subprocess.run(
                ["uv", "venv", str(venv), "--python", sys.executable],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            _done[0] = True; t.join()
            print(f"{clear_line}{PREFIX}{green}Venv created {green}✔{white}")
        except subprocess.CalledProcessError as e:
            _done[0] = True; t.join()
            print(f"{clear_line}{PREFIX}{red}Failed to create venv: {white}{light_grey}{e}{white}")
            sys.exit(1)

    _install_core_deps()


def _venv_python() -> Path:
    venv = cfg.VENV_DIR
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _uvicorn_bin() -> Path | None:
    if sys.platform == "win32":
        p = cfg.VENV_DIR / "Scripts" / "uvicorn.exe"
    else:
        p = cfg.VENV_DIR / "bin" / "uvicorn"
    return p if p.exists() else None


def _install_core_deps():
    """
    Install core server dependencies (fastapi, uvicorn, requests) into the venv.
    The etna package itself is NOT installed into the venv — it's made available
    via PYTHONPATH pointing to wherever etna-mcp is installed on the system.
    """
    from etna.kit_manager import _install_requirements
    _install_requirements(CORE_DEPS, kit_name="Etna core")
    _write_utils_shim()
    cfg.kits_dir()


def _write_utils_shim():
    """
    Write a top-level utils.py into the venv's site-packages so that
    kit files can do 'from utils import tool' regardless of working directory.
    """
    import glob
    venv = cfg.VENV_DIR
    # Find site-packages inside the venv
    pattern = str(venv / "lib" / "python*" / "site-packages")
    matches = glob.glob(pattern)
    if not matches:
        # Windows layout
        matches = glob.glob(str(venv / "Lib" / "site-packages"))
    if not matches:
        return
    site_packages = Path(matches[0])
    shim = site_packages / "utils.py"
    shim.write_text(
        "# Etna utils shim — allows kits to do 'from utils import tool'\n"
        "from etna.utils.registry import (\n"
        "    tool, get_tools, get_tools_for_kit,\n"
        "    extract_parameters, build_tool_schema, TOOLS,\n"
        ")\n"
    )


# ── Port helpers ──────────────────────────────────────────────────────────────

def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("localhost", port)) == 0


# ── Kit unload helper ─────────────────────────────────────────────────────────

def unload_kit(kit_stem: str, config: dict):
    """
    Tell the running server to remove a kit's tools from the registry.
    """
    import json, urllib.request, urllib.error

    main_port = config.get("port")
    if main_port and _port_open(main_port):
        url = f"http://localhost:{main_port}/unload_kit"
        body = json.dumps({"kit_stem": kit_stem}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass


# ── Main server start / stop ──────────────────────────────────────────────────

def _is_etna_server(port: int) -> bool:
    """Check if the process on this port is actually our server."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/list_kits", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _find_free_port(start: int) -> int:
    """Find the next available port starting from start."""
    port = start
    while _port_open(port):
        port += 1
    return port


def start_server(config: dict, verbose: bool = False) -> int:
    """
    Start the main Etna FastAPI server.
    Per-kit MCP scoping is handled via /mcp/<kit_stem> URL paths.
    Returns the port number the server is running on.
    """
    port = BASE_PORT

    if _port_open(port):
        if _is_etna_server(port):
            print(f"{PREFIX}{green}Server already running on {cyan}http://localhost:{port}{white}")
            return port
        # Port taken by something else — find next free one
        print(f"{PREFIX}{red}✘ Port {light_grey}{port}{red} taken{white}")
        port = _find_free_port(port + 1)
        print(f"{PREFIX}{green}✔ Using port {light_grey}{port}{green} instead{white}")

    ensure_venv()

    uvicorn = _uvicorn_bin()
    env = os.environ.copy()
    env["ETNA_CONFIG_DIR"] = str(cfg.CONFIG_DIR)

    # Make etna importable from the venv's uvicorn process.
    # __file__ is .../site-packages/etna/server_manager.py
    # so two .parent calls gives us the directory containing the etna package.
    etna_parent = str(Path(__file__).resolve().parent.parent)
    existing_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [etna_parent, existing_path]))

    if uvicorn:
        cmd = [str(uvicorn), "etna.server:app",
               "--host", "0.0.0.0", "--port", str(port),
               "--log-level", "info" if verbose else "error"]
    else:
        print(f"{PREFIX}{orange}Warning: uvicorn not found in venv, falling back to system Python{white}")
        cmd = [str(_venv_python()), "-m", "uvicorn", "etna.server:app",
               "--host", "0.0.0.0", "--port", str(port),
               "--log-level", "info" if verbose else "error"]

    import tempfile as _tf
    stderr_file = open(_tf.mktemp(), 'w') if not verbose else None

    proc = subprocess.Popen(
        cmd, env=env,
        stdout=subprocess.DEVNULL if not verbose else None,
        stderr=stderr_file if not verbose else None,
    )
    cfg.PID_FILE.write_text(str(proc.pid))

    hide_cursor()
    try:
        for i in range(40):
            time.sleep(0.25)
            _, _, bar, _ = progress_bar(i, 40, separate=True)
            spin = throbber(i)
            print(f"{clear_line}{white}{spin}{white} {orange}Starting server...{white} {bar}{white}", end="\r")
            if _port_open(port):
                print(f"{clear_line}{PREFIX}{light_green}Server running on {cyan}http://localhost:{port}{white}")
                print(f"{PREFIX}{light_green}Etna protocol: {cyan}http://localhost:{port}/list_kits{white}")
                print(f"{PREFIX}{light_green}MCP protocol:  {cyan}http://localhost:{port}/mcp{white}")
                if stderr_file and not stderr_file.closed:
                    stderr_file.close()
                return port
        else:
            # Server didn't start — read stderr and decide if we can self-repair
            err = ""
            if stderr_file:
                stderr_file.flush()
                stderr_file.close()
                try:
                    with open(stderr_file.name) as f:
                        err = f.read().strip()
                except Exception:
                    pass

            _venv_errors = (
                "no module named",
                "importerror",
                "cannot import",
                "modulenotfounderror",
                "_pydantic_core",
                "so: cannot open",
                "invalid elf",
            )
            is_venv_broken = any(e in err.lower() for e in _venv_errors)

            if is_venv_broken:
                print(f"{clear_line}{PREFIX}{orange}Broken venv detected — rebuilding...{white}")
                show_cursor()
                # Nuke the venv and rebuild
                import shutil as _shutil
                if cfg.VENV_DIR.exists():
                    _shutil.rmtree(cfg.VENV_DIR)
                ensure_venv()
                hide_cursor()
                # Retry launching
                proc2 = subprocess.Popen(
                    cmd, env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                cfg.PID_FILE.write_text(str(proc2.pid))
                for i in range(40):
                    time.sleep(0.25)
                    _, _, bar, _ = progress_bar(i, 40, separate=True)
                    spin = throbber(i)
                    print(f"{clear_line}{white}{spin}{white} {orange}Starting server...{white} {bar}{white}", end="\r")
                    if _port_open(port):
                        print(f"{clear_line}{PREFIX}{light_green}Server running on {cyan}http://localhost:{port}{white}")
                        print(f"{PREFIX}{light_green}Etna protocol: {cyan}http://localhost:{port}/list_kits{white}")
                        print(f"{PREFIX}{light_green}MCP protocol:  {cyan}http://localhost:{port}/mcp{white}")
                        return port
                print(f"{clear_line}{PREFIX}{red}Server failed to start after venv rebuild.{white}")
            else:
                print(f"{clear_line}{PREFIX}{red}Server failed to start.{white}")
                if err:
                    lines = [l for l in err.splitlines() if l.strip() and not l.startswith(" ")]
                    print(f"{PREFIX}{red}{lines[-1] if lines else err[-200:]}{white}")
    finally:
        show_cursor()
        if stderr_file and not stderr_file.closed:
            stderr_file.close()

    return port


def stop_server(config: dict):
    """Stop the Etna server process."""
    pid_file = cfg.PID_FILE
    if not pid_file.exists():
        print(f"{PREFIX}{red}Server: {white}{light_grey}no PID file found.{white}")
        return

    try:
        pid = int(pid_file.read_text().strip())
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
        pid_file.unlink(missing_ok=True)
        config["port"] = None
        print(f"{PREFIX}{green}Server stopped.{white}")
    except Exception as e:
        print(f"{PREFIX}{red}Could not stop server: {white}{light_grey}{e}{white}")


# ── OS service registration ───────────────────────────────────────────────────

def install_service():
    system = platform.system()
    etna_bin = shutil.which("etna")
    if not etna_bin:
        print(f"{PREFIX}{red}Could not find the {white}'{grey}etna{white}' {red}executable on PATH.{white}")
        sys.exit(1)

    if system == "Linux":
        _install_systemd(etna_bin)
    elif system == "Darwin":
        _install_launchd(etna_bin)
    elif system == "Windows":
        _install_task_scheduler(etna_bin)
    else:
        print(f"{PREFIX}{orange}Unsupported OS for service install: {white}{light_grey}{system}{white}")


def _install_systemd(etna_bin: str):
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "etna.service"
    content = f"""[Unit]
Description=Etna MCP Tool Server
After=network.target

[Service]
ExecStart={etna_bin} start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    service_file.write_text(content)
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["systemctl", "--user", "enable", "etna"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"{PREFIX}{green}systemd user service installed and enabled {green}✔{white}")
    print(f"{PREFIX}{bright_yellow}Run: {white}{grey}systemctl --user start etna{white} {bright_yellow}to start it now.{white}")


def _install_launchd(etna_bin: str):
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_file = agents_dir / "net.etna-mcp.etna.plist"
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>net.etna-mcp.etna</string>
    <key>ProgramArguments</key>
    <array>
        <string>{etna_bin}</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""
    plist_file.write_text(content)
    subprocess.run(["launchctl", "load", str(plist_file)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"{PREFIX}{green}LaunchAgent installed and loaded {green}✔{white}")


def _install_task_scheduler(etna_bin: str):
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{etna_bin}</Command>
      <Arguments>start</Arguments>
    </Exec>
  </Actions>
</Task>
"""
    tmp = cfg.CONFIG_DIR / "_etna_task.xml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(xml, encoding="utf-16")
    subprocess.run(
        ["schtasks", "/Create", "/TN", "EtnaMCPServer", "/XML", str(tmp), "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    tmp.unlink(missing_ok=True)
    print(f"{PREFIX}{green}Task Scheduler entry created {green}✔{white}")
