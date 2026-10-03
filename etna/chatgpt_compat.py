"""Personal local plugins for ChatGPT Desktop; no installed cache is edited."""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

MARKER = ".etna-plugin.json"
PLUGIN_VERSION = "1.0.1"


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".etna-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_launcher(directory: Path, executable: str, stem: str) -> str:
    """Write a package-contained launcher accepted by the portable plugin schema."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher = directory / "etna-stdio.cmd"
        content = "@echo off\r\n" + subprocess.list2cmdline(
            [executable, "start", "stdio", stem]
        ) + "\r\n"
    else:
        launcher = directory / "etna-stdio"
        content = (
            "#!/bin/sh\nexec "
            + " ".join(shlex.quote(value) for value in
                       [executable, "start", "stdio", stem])
            + "\n"
        )

    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".etna-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        os.chmod(temporary, 0o755)
        os.replace(temporary, launcher)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return "./" + launcher.name


def _owner(path: Path):
    if path.is_symlink() or not path.is_dir():
        return None
    try:
        marker = json.loads((path / MARKER).read_text(encoding="utf-8"))
        if marker.get("owner") == "etna" and isinstance(marker.get("kit_stem"), str):
            return marker["kit_stem"]
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _plugin_name(stem: str):
    slug = re.sub(r"[^a-z0-9-]+", "-", stem.lower()).strip("-")[:50] or "kit"
    # Distinguish stems whose spelling collapses to the same kebab-case name.
    digest = hashlib.sha256(stem.encode()).hexdigest()[:12]
    return f"etna-{slug}-{digest}"


def sync(config: dict, home: Path | None = None) -> Path:
    """Validate before editing; preserve entries and directories Etna does not own.

    Identity is the kit stem in the ownership marker, never a display name.
    Paths in the personal marketplace are relative to the user's home.
    """
    home = Path(os.path.abspath(home or Path.home()))
    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    plugins_root = home / ".codex" / "plugins"
    marketplace = {"name": "local-etna", "plugins": []}
    if marketplace_path.is_symlink():
        raise ValueError(f"Marketplace is a symlink: {marketplace_path}; refusing to overwrite")
    if marketplace_path.exists():
        try:
            marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read marketplace {marketplace_path}: {exc}") from exc
    if (not isinstance(marketplace, dict)
            or not isinstance(marketplace.get("name"), str)
            or not marketplace["name"]
            or not isinstance(marketplace.get("plugins"), list)
            or any(not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                   or not isinstance(entry.get("source"), (dict, str))
                   for entry in marketplace["plugins"])):
        raise ValueError(f"Malformed marketplace {marketplace_path}; refusing to overwrite")

    managed = {}
    if plugins_root.exists():
        for directory in plugins_root.iterdir():
            stem = _owner(directory)
            if stem is not None:
                managed[directory] = stem

    def owned_entry(entry):
        source = entry.get("source")
        if not isinstance(source, dict) or source.get("source") != "local":
            return False
        path = source.get("path")
        if not isinstance(path, str):
            return False
        # Do not resolve symlinks: only the exact managed package path is owned.
        return Path(os.path.abspath(home / path)) in managed

    preserved = [entry for entry in marketplace["plugins"] if not owned_entry(entry)]
    names = {entry["name"] for entry in preserved}
    packages = []
    for stem, info in config.get("kits", {}).items():
        name = _plugin_name(stem)
        directory = plugins_root / name
        if name in names or ((directory.exists() or directory.is_symlink())
                             and managed.get(directory) != stem):
            raise ValueError(f"Plugin collision at {directory}; refusing to overwrite")
        launcher_name = "etna-stdio.cmd" if os.name == "nt" else "etna-stdio"
        for filename in (MARKER, "plugin.json", "mcp.json", launcher_name):
            target = directory / filename
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f"Unsafe plugin file {target}; refusing to overwrite")
        packages.append((stem, info, name, directory))

    executable = shutil.which("etna") or "etna"
    entries = []
    for stem, info, name, directory in packages:
        display = info.get("kit_name") or stem
        description = info.get("kit_description") or f"Etna tools from {display}"
        command = _write_launcher(directory, executable, stem)
        _write_json(directory / MARKER, {"owner": "etna", "kit_stem": stem})
        _write_json(directory / "plugin.json", {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "name": name, "version": PLUGIN_VERSION, "description": description,
            "extensions": {"com.openai": {"interface": {
                "displayName": display, "shortDescription": description,
            }}},
        })
        _write_json(directory / "mcp.json", {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {name: {"type": "stdio", "command": command}},
        })
        entries.append({
            "name": name,
            "source": {"source": "local", "path": "./" + directory.relative_to(home).as_posix()},
            "interface": {"displayName": display},
            "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        })
    marketplace["plugins"] = preserved + entries
    _write_json(marketplace_path, marketplace)
    keep = {directory for _, _, _, directory in packages}
    for directory in managed:
        if directory not in keep:
            shutil.rmtree(directory)
    return marketplace_path
