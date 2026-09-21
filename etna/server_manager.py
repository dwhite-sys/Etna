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
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import csv
from pathlib import Path

from etna import config as cfg
from etna.console import *

BASE_PORT = 8467


# ── Venv self-repair ──────────────────────────────────────────────────────────

CORE_DEPS = ["fastapi>=0.111.0", "uvicorn[standard]>=0.29.0", "requests>=2.31.0"]


def _uv_command() -> list[str]:
    """Invoke uv through the exact Python interpreter running Etna."""
    return [sys.executable, "-m", "uv"]


def ensure_venv(refresh: bool = False):
    """
    Create the Etna venv if it doesn't exist, then install core dependencies.
    Uses UV for both operations. Skips dep install if uvicorn is already present.
    """
    venv = cfg.VENV_DIR

    # A copied/moved Python install can leave a venv directory behind with a dead
    # interpreter. Treat that as repairable state, not as a valid runtime.
    if venv.exists():
        try:
            subprocess.run(
                [str(_venv_python()), "-c", "import sys; print(sys.executable)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            shutil.rmtree(venv, ignore_errors=True)

    # On ordinary starts, a healthy runtime only needs the Etna code refreshed.
    # ``etna init`` passes refresh=True so dependency constraints are re-synced too.
    uvicorn = _uvicorn_bin()
    if not refresh and venv.exists() and uvicorn and uvicorn.exists():
        _sync_runtime_package()
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
                _uv_command() + ["venv", str(venv), "--python", sys.executable],
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
    """Install core server dependencies and Etna itself into the managed runtime."""
    from etna.kit_manager import _install_requirements
    if not _install_requirements(CORE_DEPS, kit_name="Etna core"):
        raise RuntimeError("Failed to install Etna core dependencies")
    _sync_runtime_package()
    _write_utils_shim()
    cfg.kits_dir()


def _venv_site_packages() -> Path:
    """Return the managed runtime's purelib directory."""
    result = subprocess.run(
        [str(_venv_python()), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


def _sync_runtime_package():
    """
    Copy the currently installed Etna package into the managed runtime.

    This deliberately avoids making the background service depend on the shell's
    PATH, the original console-script wrapper, or the Python environment that ran
    ``etna init``. Re-running init refreshes the managed copy atomically enough for
    normal upgrades: copy to a sibling temp directory, then replace the old copy.
    """
    src = Path(__file__).resolve().parent
    site_packages = _venv_site_packages()
    dst = site_packages / "etna"

    if dst.exists() and src == dst.resolve():
        return

    tmp = site_packages / ".etna-runtime-new"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if dst.exists():
        shutil.rmtree(dst)
    tmp.replace(dst)


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
    """Check if the process on this port identifies itself as Etna."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2) as r:
            if r.status != 200:
                return False
            payload = json.loads(r.read().decode("utf-8"))
            return payload.get("service") == "etna-mcp" and payload.get("status") == "ok"
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

    env = os.environ.copy()
    env["ETNA_CONFIG_DIR"] = str(cfg.CONFIG_DIR)

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


# ── Foreground service entrypoint ─────────────────────────────────────────────

def run_server_foreground(verbose: bool = False) -> int:
    """Run the Etna HTTP server in this process for an OS service manager."""
    import uvicorn

    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg.PID_FILE.write_text(str(os.getpid()))

    config = cfg.load()
    config["port"] = BASE_PORT
    cfg.save(config)

    try:
        uvicorn.run(
            "etna.server:app",
            host="0.0.0.0",
            port=BASE_PORT,
            log_level="info" if verbose else "error",
        )
    finally:
        try:
            if cfg.PID_FILE.exists() and cfg.PID_FILE.read_text().strip() == str(os.getpid()):
                cfg.PID_FILE.unlink()
        except OSError:
            pass
    return 0


def wait_for_server(timeout: float = 15.0) -> bool:
    """Wait until the Etna identity endpoint responds on the canonical port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_etna_server(BASE_PORT):
            return True
        time.sleep(0.25)
    return False


# ── OS service registration ───────────────────────────────────────────────────

def service_registered() -> bool:
    if sys.platform.startswith("linux"):
        return (Path.home() / ".config" / "systemd" / "user" / "etna.service").exists()
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / "net.etna-mcp.etna.plist").exists()
    if sys.platform == "win32":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", "EtnaMCPServer"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    return False


def _run_checked(cmd: list[str], *, description: str):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"{description}: {detail}")
    return result


def install_service():
    """Create or repair the current user's Etna startup service and start it now."""
    ensure_venv(refresh=True)
    # Keep the venv launcher path itself. On POSIX it normally symlinks to
    # the underlying interpreter, but invoking through the venv path is what
    # gives the service its managed Etna environment.
    runtime_python = _venv_python()
    system = platform.system()

    if system == "Linux":
        _install_systemd(runtime_python)
    elif system == "Darwin":
        _install_launchd(runtime_python)
    elif system == "Windows":
        _install_task_scheduler(runtime_python)
    else:
        raise RuntimeError(f"Unsupported OS for service install: {system}")


def _systemd_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _install_systemd(runtime_python: Path):
    if not shutil.which("systemctl"):
        raise RuntimeError("systemctl was not found; Etna requires a systemd user session on Linux")

    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "etna.service"
    content = f"""[Unit]
Description=Etna MCP Tool Server
After=network.target

[Service]
Type=simple
ExecStart={_systemd_quote(str(runtime_python))} -m etna _serve
Environment={_systemd_quote('ETNA_CONFIG_DIR=' + str(cfg.CONFIG_DIR))}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    subprocess.run(
        ["systemctl", "--user", "stop", "etna.service"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Vulcan versions predating Etna's native `init` lifecycle installed this
    # drop-in for the old daemonizing `etna start` service. It overrides the
    # foreground service semantics used by current Etna, so migrate it away.
    legacy_dropin = (
        service_dir
        / "etna.service.d"
        / "10-vulcan-runtime.conf"
    )
    if legacy_dropin.exists():
        legacy_dropin.unlink()
        try:
            legacy_dropin.parent.rmdir()
        except OSError:
            pass

    service_file.write_text(content, encoding="utf-8")
    _run_checked(["systemctl", "--user", "daemon-reload"], description="systemd daemon-reload failed")
    _run_checked(["systemctl", "--user", "enable", "etna.service"], description="Could not enable Etna")
    _run_checked(["systemctl", "--user", "start", "etna.service"], description="Could not start Etna")
    print(f"{PREFIX}{green}systemd user service installed and started {green}✔{white}")


def _install_launchd(runtime_python: Path):
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_file = agents_dir / "net.etna-mcp.etna.plist"
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/net.etna-mcp.etna"

    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_file)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    payload = {
        "Label": "net.etna-mcp.etna",
        "ProgramArguments": [str(runtime_python), "-m", "etna", "_serve"],
        "EnvironmentVariables": {"ETNA_CONFIG_DIR": str(cfg.CONFIG_DIR)},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
    }
    with open(plist_file, "wb") as f:
        plistlib.dump(payload, f, sort_keys=False)

    _run_checked(["launchctl", "bootstrap", domain, str(plist_file)], description="Could not bootstrap Etna LaunchAgent")
    _run_checked(["launchctl", "kickstart", "-k", service], description="Could not start Etna LaunchAgent")
    print(f"{PREFIX}{green}LaunchAgent installed and started {green}✔{white}")


def _windows_user_sid() -> str:
    result = _run_checked(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        description="Could not determine the current Windows user SID",
    )
    row = next(csv.reader([result.stdout.strip()]))
    if len(row) < 2 or not row[1].startswith("S-"):
        raise RuntimeError("Could not parse the current Windows user SID")
    return row[1]


def _install_task_scheduler(runtime_python: Path):
    from xml.sax.saxutils import escape

    sid = escape(_windows_user_sid())
    python_exe = escape(str(runtime_python))
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{sid}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>-m etna _serve</Arguments>
    </Exec>
  </Actions>
</Task>
'''
    tmp = cfg.CONFIG_DIR / "_etna_task.xml"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(xml, encoding="utf-16")
    try:
        subprocess.run(
            ["schtasks", "/End", "/TN", "EtnaMCPServer"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _run_checked(
            ["schtasks", "/Create", "/TN", "EtnaMCPServer", "/XML", str(tmp), "/F"],
            description="Could not create Etna scheduled task",
        )
        _run_checked(
            ["schtasks", "/Run", "/TN", "EtnaMCPServer"],
            description="Could not start Etna scheduled task",
        )
    finally:
        tmp.unlink(missing_ok=True)
    print(f"{PREFIX}{green}Task Scheduler entry installed and started {green}✔{white}")
