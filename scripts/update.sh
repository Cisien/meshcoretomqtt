#!/bin/bash
# ============================================================================
# MeshCore to MQTT - Update Bootstrap
# Downloads the Python installer package and runs `python3 -m installer update`
# ============================================================================
set -e

REPO="${MCTOMQTT_REPO:-Cisien/meshcoretomqtt}"
BRANCH="${MCTOMQTT_BRANCH:-main}"
EXTRA_ARGS=()

# Try to read repo/branch from existing config
INSTALL_DIR="${MCTOMQTT_INSTALL_DIR:-/opt/mctomqtt}"
CONFIG_DIR="${MCTOMQTT_CONFIG_DIR:-/etc/mctomqtt}"
if [ -f "$CONFIG_DIR/config.d/00-user.toml" ]; then
    _repo=$(python3 -c "
import tomllib
with open('$CONFIG_DIR/config.d/00-user.toml', 'rb') as f:
    c = tomllib.load(f)
print(c.get('update', {}).get('repo', ''))
" 2>/dev/null || true)
    _branch=$(python3 -c "
import tomllib
with open('$CONFIG_DIR/config.d/00-user.toml', 'rb') as f:
    c = tomllib.load(f)
print(c.get('update', {}).get('branch', ''))
" 2>/dev/null || true)
    [ -n "$_repo" ] && REPO="$_repo"
    [ -n "$_branch" ] && BRANCH="$_branch"
fi

while [[ $# -gt 0 ]]; do
    case $1 in
        --repo)   REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        *)        EXTRA_ARGS+=("$1"); shift ;;
    esac
done

# Ensure running as root (skip for --help so argparse can respond)
_needs_root=true
for arg in "${EXTRA_ARGS[@]}"; do
    [ "$arg" = "--help" ] || [ "$arg" = "-h" ] && _needs_root=false
done
if [ "$_needs_root" = true ] && [ "$(id -u)" -ne 0 ]; then
    echo "This installer requires root privileges. Re-running with sudo..."
    exec sudo bash "$0" "$@"
fi

# Check Python 3.11+
py_version=$(python3 -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}")' 2>/dev/null || true)
if [ -z "$py_version" ] || [ "$(printf '%s\n' "3.11" "$py_version" | sort -V | head -1)" != "3.11" ]; then
    echo "Error: Python 3.11+ required (found: ${py_version:-none})"
    exit 1
fi

# Download installer package to temp dir
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

if [ -n "$LOCAL_INSTALL" ]; then
    cp -r "$LOCAL_INSTALL/installer" "$TMP_DIR/installer"
else
    # TODO: Switch to downloading GitHub Releases once CI/CD creates tagged releases
    ARCHIVE_URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.zip"
    echo "Downloading repository archive..."
    curl -fsSL --retry 3 --retry-delay 2 -o "$TMP_DIR/repo.zip" "$ARCHIVE_URL" || {
        echo "Error: Failed to download repository archive"; exit 1
    }
    REPO_NAME=$(echo "$REPO" | cut -d'/' -f2)
    BRANCH_SANITIZED=$(echo "$BRANCH" | tr '/' '-')
    unzip -q "$TMP_DIR/repo.zip" -d "$TMP_DIR" || {
        echo "Error: Failed to extract repository archive"; exit 1
    }
    rm -f "$TMP_DIR/repo.zip"
    cp -r "$TMP_DIR/$REPO_NAME-$BRANCH_SANITIZED/installer" "$TMP_DIR/installer"
fi

# Run Python updater
export INSTALL_REPO="$REPO"
export INSTALL_BRANCH="$BRANCH"
cd "$TMP_DIR"
python3 -m installer update "${EXTRA_ARGS[@]}"
