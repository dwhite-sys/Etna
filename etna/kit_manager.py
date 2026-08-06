"""
etna/kit_manager.py — Kit install, update, remove, and metadata parsing

Supports two install formats:
  .py   — bare kit file, installed directly
  .ekp  — zip archive containing <stem>.py and optionally a skill/ folder;
           the kit file is installed as normal and the skill/ folder is
           extracted to ~/.etna_server/kit_skills/<stem>/
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import io
import zipfile
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

def _url_exists(url: str) -> bool:
    """HEAD check — returns True if the URL responds with 200."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


REPO_MANIFEST_URL = "https://raw.githubusercontent.com/dwhite-sys/etna-kits/main/manifest.json"
REPO_VERSION_URL  = "https://raw.githubusercontent.com/dwhite-sys/etna-kits/main/kits/{stem}/versions/{version}/{stem}.py"


def _parse_name_version(name: str) -> tuple[str, str | None]:
    """
    Split 'ntfy==1.0.0b1' into ('ntfy', '1.0.0b1').
    Returns (name, None) if no version specifier present.
    """
    if "==" in name:
        parts = name.split("==", 1)
        return parts[0].strip(), parts[1].strip()
    return name.strip(), None


def resolve_kit_from_repo(name: str) -> tuple[str, str] | None:
    """
    Resolve a kit name (optionally with ==version) to (stem, url).
    If a version is specified, constructs the URL directly from the repo path
    pattern without requiring it to be the current manifest version.
    """
    import threading as _threading, time as _time

    bare_name, requested_version = _parse_name_version(name)

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
        if entry.get("name", "").lower() == bare_name.lower() or \
                entry.get("stem", "").lower() == bare_name.lower():
            stem = entry["stem"]

            if requested_version is not None:
                # Validate version string is PEP 440 compliant
                try:
                    from packaging.version import Version, InvalidVersion
                    Version(requested_version)
                except Exception:
                    print(f"{clear_line}{PREFIX}{red}Invalid version: {white}'{grey}{requested_version}{white}' "
                          f"{red}(must be PEP 440, e.g. 1.0.0, 1.0.0b1){white}")
                    return None

                # Try .ekp path first for versioned installs, fall back to .py
                ekp_url = REPO_VERSION_URL.format(stem=stem, version=requested_version).replace(f"{stem}.py", f"{stem}.ekp")
                py_url  = REPO_VERSION_URL.format(stem=stem, version=requested_version)
                url = ekp_url if _url_exists(ekp_url) else py_url
                print(f"{clear_line}{PREFIX}{green}✔ Resolved {white}'{grey}{stem}{white}' "
                      f"{grey}=={white} {light_grey}{requested_version}{white}")
            else:
                # Prefer .ekp over .py when available — includes skill
                url = entry.get("ekp_url") or entry["url"]
                version = entry.get("version", "")
                print(f"{clear_line}{PREFIX}{green}✔ Resolved {white}'{grey}{stem}{white}' "
                      f"{grey}=={white} {light_grey}{version}{white}")

            return stem, url

    print(f"{clear_line}{PREFIX}{red}Kit {white}'{grey}{bare_name}{white}' {red}not found in kit index.{white}")
    return None


# ── EKP unpacking ─────────────────────────────────────────────────────────────

def _install_ekp(ekp_path: Path, config: dict, kits_dir: Path,
                 is_update: bool = False) -> str | None:
    """
    Unpack a .ekp archive and install the kit and optional skill.

    .ekp structure:
      <stem>.py          — required: the kit file
      skill/             — optional: skill folder (SKILL.md + subdirs)
        SKILL.md
        scripts/
        references/
        assets/
    """
    if not zipfile.is_zipfile(ekp_path):
        print(f"{PREFIX}{red}Not a valid .ekp archive: {white}{light_grey}{ekp_path}{white}")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)

        with zipfile.ZipFile(ekp_path) as zf:
            zf.extractall(tmp)

        # Find the kit .py file — must be a top-level .py
        py_files = list(tmp.glob("*.py"))
        if not py_files:
            print(f"{PREFIX}{red}.ekp archive contains no .py kit file.{white}")
            sys.exit(1)
        if len(py_files) > 1:
            print(f"{PREFIX}{red}.ekp archive contains multiple .py files — expected exactly one.{white}")
            sys.exit(1)

        kit_py = py_files[0]
        kit_stem = kit_py.stem

        # Install the kit file
        result = install_kit(str(kit_py), config, kits_dir, is_update=is_update)
        if result is None:
            return None

        # Install the skill folder if present
        skill_src = tmp / "skill"
        if skill_src.exists() and skill_src.is_dir():
            skill_md = skill_src / "SKILL.md"
            if not skill_md.exists():
                print(f"{PREFIX}{orange}Warning: .ekp skill/ folder has no SKILL.md — skill not installed.{white}")
            else:
                dest = cfg.kit_skill_path(kit_stem)
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_src, dest)
                print(f"{PREFIX}{light_green}Installed skill for {white}'{grey}{kit_stem}{white}'")

        return result


# ── Install / Update ──────────────────────────────────────────────────────────

def install_kit(kit_path_str: str, config: dict, kits_dir: Path,
                is_update: bool = False) -> str | None:
    """
    Copy a kit file into the managed kits directory and register it in config.
    Accepts .py files or .ekp archives.
    If already exists and is_update=False, prompt the user.
    Returns the kit stem on success, None on abort.
    """
    kit_path = Path(kit_path_str).resolve()

    if not kit_path.exists():
        print(f"{PREFIX}{red}File not found: {white}{light_grey}{kit_path}{white}")
        sys.exit(1)

    # Delegate .ekp archives to their own handler
    if kit_path.suffix == ".ekp":
        return _install_ekp(kit_path, config, kits_dir, is_update=is_update)

    if kit_path.suffix != ".py":
        print(f"{PREFIX}{red}File must be a .py or .ekp file: {white}{light_grey}{kit_path}{white}")
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
    ext = ".ekp" if url.endswith(".ekp") else ".py"
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / f"{kit_stem}{ext}"
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
            t.join()
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

    # Remove associated kit skill if present
    kit_skill = cfg.kit_skill_path(kit_name)
    if kit_skill.exists():
        shutil.rmtree(kit_skill, ignore_errors=True)

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
                elif ll.startswith("prepared"):
                    try: n = int(ll.split()[1])
                    except: n = 0
                    status = f"{cyan}↓ {n} downloading{white}"
                elif ll.startswith("installed"):
                    try: n_installed = int(ll.split()[1])
                    except: n_installed = total
                    status = f"{light_green}✔ {n_installed} installed{white}"
                elif ll.startswith("checked"):
                    status = f"{light_green}✔ Already up to date{white}"
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


# ── Skill management ──────────────────────────────────────────────────────────

REPO_SKILL_URL = "https://raw.githubusercontent.com/dwhite-sys/etna-kits/main/skills/{stem}/versions/{version}/{stem}.skill"


def parse_skill_meta(skill_dir: Path) -> dict:
    """Parse name and description from a skill folder's SKILL.md frontmatter."""
    skill_md = skill_dir / "SKILL.md"
    meta = {"name": skill_dir.name, "description": ""}
    if not skill_md.exists():
        return meta
    content = skill_md.read_text(encoding="utf-8")
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            for line in content[3:end].strip().splitlines():
                if line.startswith("name:"):
                    meta["name"] = line[5:].strip()
                elif line.startswith("description:"):
                    meta["description"] = line[12:].strip()
    return meta


def list_installed_skills() -> list[dict]:
    """Return metadata for all installed general skills."""
    skills_root = cfg.SKILLS_DIR
    if not skills_root.exists():
        return []
    results = []
    for skill_dir in sorted(skills_root.iterdir()):
        if skill_dir.is_dir():
            meta = parse_skill_meta(skill_dir)
            meta["stem"] = skill_dir.name
            results.append(meta)
    return results


def install_skill(skill_path_str: str) -> str | None:
    """
    Install a general skill from a .skill archive or a folder.
    Extracts to ~/.etna_server/skills/<stem>/
    Returns the skill stem on success, None on failure.
    """
    skill_path = Path(skill_path_str).resolve()

    if not skill_path.exists():
        print(f"{PREFIX}{red}Not found: {white}{light_grey}{skill_path}{white}")
        return None

    if skill_path.is_dir():
        # Direct folder install — copy it
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            print(f"{PREFIX}{red}No SKILL.md found in {white}{light_grey}{skill_path}{white}")
            return None
        stem = skill_path.name
        dest = cfg.skills_dir() / stem
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_path, dest)
        meta = parse_skill_meta(dest)
        print(f"{PREFIX}{light_green}Installed skill {white}'{grey}{meta['name']}{white}' {light_green}({grey}{stem}{white})")
        return stem

    if skill_path.suffix != ".skill":
        print(f"{PREFIX}{red}File must be a .skill archive or a folder: {white}{light_grey}{skill_path}{white}")
        return None

    if not zipfile.is_zipfile(skill_path):
        print(f"{PREFIX}{red}Not a valid .skill archive: {white}{light_grey}{skill_path}{white}")
        return None

    stem = skill_path.stem

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        with zipfile.ZipFile(skill_path) as zf:
            zf.extractall(tmp)

        # .skill archives contain a skill/ folder
        skill_src = tmp / "skill"
        if not skill_src.exists():
            # Fallback: treat root of archive as the skill folder
            skill_src = tmp

        skill_md = skill_src / "SKILL.md"
        if not skill_md.exists():
            print(f"{PREFIX}{red}.skill archive has no SKILL.md{white}")
            return None

        dest = cfg.skills_dir() / stem
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_src, dest)

    meta = parse_skill_meta(cfg.skills_dir() / stem)
    print(f"{PREFIX}{light_green}Installed skill {white}'{grey}{meta['name']}{white}' {light_green}({grey}{stem}{white})")
    return stem


def remove_skill(skill_stem: str) -> bool:
    """Remove an installed general skill. Returns True on success."""
    skill_dir = cfg.skills_dir() / skill_stem
    if not skill_dir.exists():
        # Try matching by name
        for d in cfg.SKILLS_DIR.iterdir():
            if d.is_dir():
                meta = parse_skill_meta(d)
                if meta["name"].lower() == skill_stem.lower():
                    skill_dir = d
                    skill_stem = d.name
                    break
        else:
            print(f"{PREFIX}{red}Skill not found: {white}'{grey}{skill_stem}{white}'")
            return False

    meta = parse_skill_meta(skill_dir)
    answer = input(
        f"{PREFIX}{red}Remove skill {white}'{grey}{meta['name']}{white}'{red}? "
        f"{white}[{green}y{white}/{red}n{white}]: "
    ).strip().lower()
    if answer != "y":
        print(f"{PREFIX}{grey}Aborted.{white}")
        return False

    shutil.rmtree(skill_dir, ignore_errors=True)
    print(f"{PREFIX}{red}Removed skill {white}'{grey}{meta['name']}{white}'")
    return True


def resolve_skill_from_repo(name: str) -> tuple[str, str] | None:
    """Resolve a skill name (optionally with ==version) to (stem, url)."""
    import threading as _threading, time as _time

    bare_name, requested_version = _parse_name_version(name)

    _done = [False]
    def _spin():
        tick = 0
        while not _done[0]:
            print(f"{clear_line}{PREFIX}{yellow}{throbber(tick)} Fetching index{white}...", end="\r")
            tick += 1; _time.sleep(0.1)

    t = _threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(REPO_MANIFEST_URL, timeout=10) as resp:
            manifest = json.loads(resp.read())
        _done[0] = True; t.join()
    except Exception as e:
        _done[0] = True; t.join()
        print(f"{clear_line}{PREFIX}{red}Could not fetch index: {white}{light_grey}{e}{white}")
        return None

    for entry in manifest.get("skills", []):
        if entry.get("name", "").lower() == bare_name.lower() or \
                entry.get("stem", "").lower() == bare_name.lower():
            stem = entry["stem"]

            if requested_version is not None:
                try:
                    from packaging.version import Version
                    Version(requested_version)
                except Exception:
                    print(f"{clear_line}{PREFIX}{red}Invalid version: {white}'{grey}{requested_version}{white}'")
                    return None
                url = REPO_SKILL_URL.format(stem=stem, version=requested_version)
                print(f"{clear_line}{PREFIX}{green}✔ Resolved skill {white}'{grey}{stem}{white}' "
                      f"{grey}=={white} {light_grey}{requested_version}{white}")
            else:
                url = entry["url"]
                version = entry.get("version", "")
                print(f"{clear_line}{PREFIX}{green}✔ Resolved skill {white}'{grey}{stem}{white}' "
                      f"{grey}=={white} {light_grey}{version}{white}")
            return stem, url

    print(f"{clear_line}{PREFIX}{red}Skill {white}'{grey}{bare_name}{white}' {red}not found in index.{white}")
    return None


def install_skill_from_repo(name: str) -> str | None:
    """Resolve a skill from the repo, download it, and install it."""
    result = resolve_skill_from_repo(name)
    if result is None:
        return None

    stem, url = result

    import threading as _threading
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / f"{stem}.skill"
        try:
            _done = [False]
            def _spin():
                tick = 0
                while not _done[0]:
                    print(f"{clear_line}{PREFIX}{light_green}{throbber(tick)} Downloading {white}'{grey}{stem}{white}'{white}...", end="\r")
                    tick += 1
                    import time as _time; _time.sleep(0.1)
            t = _threading.Thread(target=_spin, daemon=True); t.start()
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
            _done[0] = True; t.join()
            tmp_path.write_bytes(data)
            print(f"{clear_line}{PREFIX}{light_green}Downloaded {white}'{grey}{stem}{white}' {green}✔{white}")
        except Exception as e:
            _done[0] = True
            print(f"{clear_line}{PREFIX}{red}Download failed: {white}{light_grey}{e}{white}")
            return None

        return install_skill(str(tmp_path))


def repo_inspect(name: str) -> dict | None:
    """
    Fetch and parse a kit or skill from the repo without downloading.
    Returns a dict with type, metadata, and content details.
    """
    import threading as _threading, time as _time

    _done = [False]
    def _spin():
        tick = 0
        while not _done[0]:
            print(f"{clear_line}{PREFIX}{yellow}{throbber(tick)} Fetching index{white}...", end="\r")
            tick += 1; _time.sleep(0.1)

    t = _threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(REPO_MANIFEST_URL, timeout=10) as resp:
            manifest = json.loads(resp.read())
        _done[0] = True; t.join()
    except Exception as e:
        _done[0] = True; t.join()
        print(f"{clear_line}{PREFIX}{red}Could not fetch index: {white}{light_grey}{e}{white}")
        return None

    # Search kits first
    for entry in manifest.get("kits", []):
        if entry.get("name", "").lower() == name.lower() or \
                entry.get("stem", "").lower() == name.lower():
            # Fetch the .py to parse tools
            url = entry.get("url", "")
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    source = resp.read().decode("utf-8")
                # Parse tools via AST
                import ast as _ast
                tree = _ast.parse(source)
                tools = []
                for node in _ast.walk(tree):
                    if not isinstance(node, _ast.FunctionDef):
                        continue
                    for dec in node.decorator_list:
                        if (isinstance(dec, _ast.Name) and dec.id == "tool") or \
                           (isinstance(dec, _ast.Attribute) and dec.attr == "tool"):
                            doc = (_ast.get_docstring(node) or "").strip().split("\n")[0]
                            tools.append({"name": node.name, "description": doc})
                            break
            except Exception:
                tools = []

            return {
                "type": "kit",
                "name": entry.get("name", ""),
                "stem": entry.get("stem", ""),
                "version": entry.get("version", ""),
                "description": entry.get("description", ""),
                "tools": tools,
                "has_skill": bool(entry.get("ekp_url")),
            }

    # Search skills
    for entry in manifest.get("skills", []):
        if entry.get("name", "").lower() == name.lower() or \
                entry.get("stem", "").lower() == name.lower():
            # Fetch the .skill to list contents
            url = entry.get("url", "")
            contents = []
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = resp.read()
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    contents = sorted(zf.namelist())
            except Exception:
                pass

            return {
                "type": "skill",
                "name": entry.get("name", ""),
                "stem": entry.get("stem", ""),
                "version": entry.get("version", ""),
                "description": entry.get("description", ""),
                "contents": contents,
            }

    return None
