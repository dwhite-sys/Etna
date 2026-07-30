"""
etna/kit_manager.py — Kit install, update, remove, and metadata parsing
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from etna import config as cfg
from etna.console import *


# ── Metadata parsing ──────────────────────────────────────────────────────────

def parse_kit_metadata(kit_path: Path) -> dict:
    """
    Parse a kit file's AST and extract module-level metadata.
    Returns defaults for any missing fields.
    """
    stem = kit_path.stem
    meta = {
        "kit_name": stem,
        "kit_description": "",
        "requirements": [],
        "config": {},
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
            key = target.id
            if key == "kit_name" and isinstance(node.value, ast.Constant):
                meta["kit_name"] = node.value.value
            elif key == "kit_description" and isinstance(node.value, ast.Constant):
                meta["kit_description"] = node.value.value
            elif key == "requirements" and isinstance(node.value, ast.List):
                meta["requirements"] = [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
            elif key == "config" and isinstance(node.value, ast.Dict):
                parsed_config = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                            and isinstance(v, ast.Constant):
                        parsed_config[k.value] = v.value
                meta["config"] = parsed_config

    return meta


# ── Repo resolution ───────────────────────────────────────────────────────────

REPO_MANIFEST_URL = "https://raw.githubusercontent.com/dwhite-sys/etna-kits/main/manifest.json"


def resolve_kit_from_repo(name: str) -> tuple[str, str] | None:
    import threading as _threading, time as _time
    _done = [False]

    def _spin():
        tick = 0
        while not _done[0]:
            print(f"{clear_line}{PREFIX}{yellow}{throbber(tick)} Fetching kit index{white}...", end="\r")
            tick += 1; _time.sleep(0.1)

    t = _threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(REPO_MANIFEST_URL, timeout=10) as resp:
            manifest = json.loads(resp.read())
        _done[0] = True; t.join()
    except Exception as e:
        _done[0] = True; t.join()
        print(f"{clear_line}{PREFIX}{red}Could not fetch kit index: {white}{light_grey}{e}{white}")
        return None

    for entry in manifest.get("kits", []):
        if entry.get("name", "").lower() == name.lower() or \
                entry.get("stem", "").lower() == name.lower():
            stem = entry["stem"]
            print(f"{clear_line}{PREFIX}{green}✔ Resolved {white}'{grey}{stem}{white}'")
            return stem, entry["url"]

    print(f"{clear_line}{PREFIX}{red}Kit {white}'{grey}{name}{white}' {red}not found in kit index.{white}")
    return None


# ── Install / Update ──────────────────────────────────────────────────────────

def install_kit(kit_path_str: str, config: dict, kits_dir: Path,
                is_update: bool = False) -> str | None:
    """
    Copy a kit file into the managed kits directory and register it in config.
    If already exists and is_update=False, prompt the user.
    Returns the kit stem on success, None on abort.
    """
    kit_path = Path(kit_path_str).resolve()

    if not kit_path.exists():
        print(f"{PREFIX}{red}File not found: {white}{light_grey}{kit_path}{white}")
        sys.exit(1)
    if kit_path.suffix != ".py":
        print(f"{PREFIX}{red}File must be a .py file: {white}{light_grey}{kit_path}{white}")
        sys.exit(1)

    # Lint before install — blocks on errors
    from etna.kit_linter import lint_kit
    if not lint_kit(kit_path):
        sys.exit(1)

    # Check for tool name collisions against installed kits
    _check_collisions(kit_path, config, kits_dir)

    meta = parse_kit_metadata(kit_path)
    kit_stem = kit_path.stem
    already_installed = kit_stem in config.get("kits", {})

    # Prompt if already installed and not explicitly an update
    if already_installed and not is_update:
        name = config["kits"][kit_stem].get("kit_name", kit_stem)
        answer = input(
            f"{PREFIX}{orange}Kit {white}'{grey}{name}{white}' "
            f"{orange}already exists. Update it? "
            f"{white}[{green}y{white}/{red}n{white}]: "
        ).strip().lower()
        if answer != "y":
            print(f"{PREFIX}{grey}Aborted.{white}")
            return None

    # Backup existing kit file during updates for rollback on dep failure
    backup = None
    dest = kits_dir / kit_path.name
    if already_installed and dest.exists():
        backup = dest.with_suffix(".py.bak")
        shutil.copy2(dest, backup)

    shutil.copy2(kit_path, dest)

    # Clear stale bytecode
    pycache = kits_dir / "__pycache__"
    for f in pycache.glob(f"{kit_stem}*.pyc"):
        f.unlink(missing_ok=True)

    config.setdefault("kits", {})[kit_stem] = {
        "kit_name": meta["kit_name"],
        "kit_description": meta["kit_description"],
        "enabled": True,
        "deps_ok": True,
    }

    # Write config defaults — preserve user-set values
    if meta["config"]:
        existing_cfg = cfg.load_kit_config(kit_stem)
        merged = {**meta["config"], **existing_cfg}
        cfg.save_kit_config(kit_stem, merged)
        print(f"{PREFIX}{green}Config written: {white}{light_grey}{list(meta['config'].keys())}{white}")

    # Install requirements
    deps_ok = True
    if meta["requirements"]:
        deps_ok = _install_requirements(meta["requirements"], kit_name=meta["kit_name"])

    if not deps_ok:
        # Rollback on dependency failure
        if backup:
            shutil.copy2(backup, dest)
            print(f"{PREFIX}{orange}Dependencies failed — rolled back to previous version.{white}")
        else:
            dest.unlink(missing_ok=True)
            config.get("kits", {}).pop(kit_stem, None)
            print(f"{PREFIX}{red}Dependencies failed — kit not installed.{white}")
        if backup:
            backup.unlink(missing_ok=True)
        return None

    if backup:
        backup.unlink(missing_ok=True)

    # Hot-reload into the main server
    _reload_kit_in_server(kit_stem, config)

    action = "Updated" if already_installed else "Installed"
    print(f"{PREFIX}{light_green}{action} kit {white}'{grey}{meta['kit_name']}{white}' "
          f"{light_green}({grey}{kit_stem}{white})")
    return kit_stem


def install_kit_from_repo(name: str, config: dict, kits_dir: Path,
                          is_update: bool = False) -> str | None:
    """Resolve a kit from the curated repo, download it, and install it."""
    result = resolve_kit_from_repo(name)
    if result is None:
        return None

    kit_stem, url = result

    # Use tempfile to avoid missing-directory errors
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / f"{kit_stem}.py"
        try:
            import threading as _threading
            _done = [False]

            def _spin():
                tick = 0
                while not _done[0]:
                    spin = throbber(tick)
                    print(f"{clear_line}{PREFIX}{light_green}{spin} Downloading {white}'{grey}{kit_stem}{white}'{white}...", end="\r")
                    tick += 1
                    import time as _time
                    _time.sleep(0.1)

            t = _threading.Thread(target=_spin, daemon=True)
            t.start()
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
            _done[0] = True
            t.join()
            tmp_path.write_bytes(data)
            print(f"{clear_line}{PREFIX}{light_green}Downloaded {white}'{grey}{kit_stem}{white}' "
                  f"{green}✔{white}")
        except Exception as e:
            _done[0] = True
            print(f"{clear_line}{PREFIX}{red}Download failed: {white}{light_grey}{e}{white}")
            return None

        return install_kit(str(tmp_path), config, kits_dir, is_update=is_update)


def remove_kit(kit_name: str, config: dict, kits_dir: Path):
    """Remove a kit from the managed kits directory, config, and running server."""
    kit_file = kits_dir / f"{kit_name}.py"
    if not kit_file.exists():
        print(f"{PREFIX}{red}Kit not found: {white}'{grey}{kit_name}{white}'")
        sys.exit(1)

    display = config.get("kits", {}).get(kit_name, {}).get("kit_name", kit_name)

    # Confirm removal
    answer = input(
        f"{PREFIX}{red}Remove kit {white}'{grey}{display}{white}'{red}? "
        f"{white}[{green}y{white}/{red}n{white}]: "
    ).strip().lower()
    if answer != "y":
        print(f"{PREFIX}{grey}Aborted.{white}")
        return

    # Unload from running server and stop its MCP process
    from etna.server_manager import unload_kit
    unload_kit(kit_name, config)

    kit_file.unlink()

    # Clear bytecode
    pycache = kits_dir / "__pycache__"
    for f in pycache.glob(f"{kit_name}*.pyc"):
        f.unlink(missing_ok=True)

    config.get("kits", {}).pop(kit_name, None)

    kit_cfg_path = cfg.kit_config_path(kit_name).parent
    if kit_cfg_path.exists():
        shutil.rmtree(kit_cfg_path, ignore_errors=True)

    print(f"{PREFIX}{red}Removed kit {white}'{grey}{display}{white}'")


# ── Collision detection ───────────────────────────────────────────────────────

def _check_collisions(kit_path: Path, config: dict, kits_dir: Path):
    """
    Warn if any @tool function names in the new kit collide with tools
    from other already-installed kits.
    """
    try:
        tree = ast.parse(kit_path.read_text(encoding="utf-8"))
    except Exception:
        return

    new_kit_stem = kit_path.stem
    new_tools = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Name) and dec.id == "tool") or \
               (isinstance(dec, ast.Attribute) and dec.attr == "tool"):
                new_tools.add(node.name)
                break

    for kit_stem in config.get("kits", {}):
        if kit_stem == new_kit_stem:
            continue
        existing_file = kits_dir / f"{kit_stem}.py"
        if not existing_file.exists():
            continue
        try:
            existing_tree = ast.parse(existing_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in ast.walk(existing_tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Name) and dec.id == "tool") or \
                   (isinstance(dec, ast.Attribute) and dec.attr == "tool"):
                    if node.name in new_tools:
                        existing_name = config["kits"][kit_stem].get("kit_name", kit_stem)
                        print(
                            f"{PREFIX}{orange}Warning: tool name {white}'{light_grey}{node.name}{white}' "
                            f"{orange}collides with a tool in {white}'{grey}{existing_name}{white}'{orange}. "
                            f"The new kit's tool will take precedence.{white}"
                        )
                    break


# ── Runtime dependency install ────────────────────────────────────────────────

def _install_requirements(requirements: list[str], kit_name: str = "") -> bool:
    """
    Install packages into the Etna venv using uv pip.
    Three-line live display:
      Line 1: progress bar (current/total) (percent%)
      Line 2: Installing deps for {main|kit_name}
      Line 3: Downloading|Installing {dep_name}...
    Returns True if all succeeded, False if any failed.
    """
    from etna.config import VENV_DIR

    venv_python = VENV_DIR / ("Scripts" if sys.platform == "win32" else "bin") / \
                  ("python.exe" if sys.platform == "win32" else "python")

    total = len(requirements)
    if total == 0:
        return True

    if kit_name == "Etna core" or not kit_name:
        kit_label = f"{light_blue}main{white}"
    else:
        kit_label = f"{grey}{kit_name}{white}"

    hide_cursor()
    try:
        failed = []
        spin_tick = 0
        n_installed = 0

        # Reserve 3 lines
        print(f"{clear_line}")
        print(f"{clear_line}")
        print(f"{clear_line}", end="\r")

        proc = subprocess.Popen(
            ["uv", "pip", "install", "--python", str(venv_python)] + requirements,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        status = f"{grey}Resolving...{white}"
        if proc.stdout:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                ll = line.lower()

                if ll.startswith("resolved"):
                    try: n = int(ll.split()[1])
                    except: n = 0
                    status = f"{grey}↻ {n} resolved{white}"
                elif ll.startswith("prepared") or ll.startswith("downloading"):
                    try: n = int(ll.split()[1])
                    except: n = 0
                    status = f"{cyan}↓ {n} downloading{white}"
                elif ll.startswith("installed"):
                    try: n_installed = int(ll.split()[1])
                    except: n_installed = total
                    status = f"{green}✔ {n_installed} installed{white}"
                elif ll.startswith("checked"):
                    status = f"{green}✔ Already up to date{white}"
                    n_installed = total
                else:
                    continue

                done = min(n_installed, total)
                pct = int((done / total) * 100) if total > 0 else 0
                _, _, bar, _ = progress_bar(done, total, separate=True)
                spin = throbber(spin_tick)
                spin_tick += 1

                print(f"{up}{up}{clear_line}{spin} {bar}{white} ({grey}{done}{white}/{grey}{total}{white}) "
                      f"({grey}{pct}%{white})", end="\n")
                print(f"{clear_line} {white}Installing deps for {kit_label}", end="\n")
                print(f"{clear_line} {status}", end="\r")

        proc.wait()
        if proc.returncode != 0:
            failed.append("one or more packages")

        # Collapse to single ✔ line
        if failed:
            print(f"{up}{up}{up}{clear_line}{orange}⚠{white} Failed to install some deps for {kit_label}", end="\n")
            print(f"{clear_line}", end="\r")
            show_cursor()
            return False
        else:
            print(f"{up}{up}{up}{clear_line}{light_green}✔{white} Installed deps for {kit_label}", end="\n")
            print(f"{clear_line}", end="\r")
            return True

    finally:
        show_cursor()


# ── Hot reload ────────────────────────────────────────────────────────────────

def _reload_kit_in_server(kit_stem: str, config: dict):
    """
    Tell the running Etna server to hot-reload a kit, then bounce the
    scoped per-kit MCP uvicorn process so OpenWebUI gets fresh tools too.
    """
    import urllib.error

    port = config.get("port")
    if not port:
        return

    url = f"http://localhost:{port}/reload_kit"
    body = json.dumps({"kit_stem": kit_stem}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    print(f"{PREFIX}{yellow}{throbber(0)} Hot-reloading {white}'{grey}{kit_stem}{white}'{white}...", end="\r")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        registered = data.get("tools_registered", [])
        print(f"{clear_line}{PREFIX}{light_green}Hot-reloaded {white}'{grey}{kit_stem}{white}'{white}: "
              f"{light_grey}{len(registered)}{white} tool(s) "
              f"({light_grey}{', '.join(registered) or 'none'}{white})")
    except urllib.error.URLError:
        print(f"{clear_line}{PREFIX}{orange}Warning: could not hot-reload kit "
              f"{white}({light_grey}server may need a restart{white})")
        return


