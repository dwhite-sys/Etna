"""
etna/stdio_shim.py — Thin bridge translating MCP JSON-RPC from stdin
to the already-running Etna server's HTTP API.

Scoping to a single kit is done via the URL path: /mcp/<kit_stem>
All shims share the same running server process.
"""

import json
import sys
import urllib.request
import urllib.error


def run_shim(port: int, kit_stem: str | None = None):
    """
    Read MCP JSON-RPC from stdin, forward to the Etna server, write response to stdout.
    If kit_stem is given, scope all requests to /mcp/<kit_stem>.
    Otherwise hits /mcp for all kits.
    """
    if kit_stem:
        base_url = f"http://localhost:{port}/mcp/{kit_stem}"
    else:
        base_url = f"http://localhost:{port}/mcp"

    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            continue

        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(base_url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = resp.read().decode()
            sys.stdout.write(result + "\n")
            sys.stdout.flush()
        except urllib.error.URLError as e:
            error_resp = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32603, "message": f"Etna server unreachable: {e}"},
            }
            sys.stdout.write(json.dumps(error_resp) + "\n")
            sys.stdout.flush()
