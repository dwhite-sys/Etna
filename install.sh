#!/bin/sh
set -eu

ETNA_PACKAGE="${ETNA_PACKAGE:-etna-mcp>=1.0.0b39}"
ETNA_PYTHON="${ETNA_PYTHON:-3.12}"

say() {
    printf '\nEtna > %s\n' "$*"
}

fail() {
    printf '\nEtna install failed: %s\n' "$*" >&2
    exit 1
}

: "${HOME:?HOME is not set}"

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi

    if [ -n "${UV_INSTALL_DIR:-}" ] && [ -x "${UV_INSTALL_DIR%/}/uv" ]; then
        printf '%s\n' "${UV_INSTALL_DIR%/}/uv"
        return 0
    fi

    if [ -n "${XDG_BIN_HOME:-}" ] && [ -x "${XDG_BIN_HOME%/}/uv" ]; then
        printf '%s\n' "${XDG_BIN_HOME%/}/uv"
        return 0
    fi

    if [ -n "${XDG_DATA_HOME:-}" ] && [ -x "${XDG_DATA_HOME%/}/../bin/uv" ]; then
        printf '%s\n' "${XDG_DATA_HOME%/}/../bin/uv"
        return 0
    fi

    if [ -x "${HOME}/.local/bin/uv" ]; then
        printf '%s\n' "${HOME}/.local/bin/uv"
        return 0
    fi

    return 1
}

case "$(uname -s)" in
    Linux|Darwin) ;;
    *) fail "This installer supports macOS and Linux. On Windows, use install.ps1." ;;
esac

UV_BIN="$(find_uv || true)"

if [ -z "$UV_BIN" ]; then
    say "Installing uv"

    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh |
            env UV_NO_MODIFY_PATH=1 sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh |
            env UV_NO_MODIFY_PATH=1 sh
    else
        fail "curl or wget is required to bootstrap uv."
    fi

    UV_BIN="$(find_uv || true)"
fi

[ -n "$UV_BIN" ] || fail "uv was installed but could not be located."

say "Installing Etna"

"$UV_BIN" tool install \
    --python "$ETNA_PYTHON" \
    --force \
    "$ETNA_PACKAGE"

TOOL_BIN="$("$UV_BIN" tool dir --bin)"
[ -n "$TOOL_BIN" ] ||
    fail "uv did not report its tool executable directory."

# Persist the uv/Etna executable directory for future shells.
#
# This is intentionally best-effort: uv can report a non-zero status when
# the shell profile already contains the correct entry but the current shell
# has not reloaded it yet. We invoke Etna by absolute path below regardless.
"$UV_BIN" tool update-shell >/dev/null 2>&1 || true

ETNA_BIN="${TOOL_BIN%/}/etna"

[ -x "$ETNA_BIN" ] ||
    fail "Etna was installed but its launcher was not found at $ETNA_BIN."

say "Initializing Etna"

"$ETNA_BIN" init

printf '\nEtna installation complete.\n'

if ! command -v etna >/dev/null 2>&1; then
    printf 'Open a new terminal before running etna directly.\n'
fi
