#!/bin/bash
# ============================================================================
# MeshCore to MQTT - switch an existing install to the CascadiaMesh version
#
#   curl -fsSL https://raw.githubusercontent.com/jade-on-mesh/meshcoretomqtt/radio2-profile/scripts/switch-to-cascadiamesh.sh | sudo bash
#
# Points updates at this repository (so later updates stay on it), adds the
# CascadiaMesh analyzer's broker unless one is configured (--no-broker skips
# it), and updates. Everything else in your config stays as it is. On a
# repeater without a second radio profile it behaves exactly like the regular
# meshcoretomqtt. Going back: set [update] repo/branch to Cisien/meshcoretomqtt
# and main in /etc/mctomqtt/config.d/99-user.toml, then update.
# ============================================================================
set -e

REPO="${MCTOMQTT_REPO:-jade-on-mesh/meshcoretomqtt}"
BRANCH="${MCTOMQTT_BRANCH:-radio2-profile}"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --repo)   REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        *)        EXTRA_ARGS+=("$1"); shift ;;
    esac
done

_needs_root=true
for arg in "${EXTRA_ARGS[@]}"; do
    [ "$arg" = "--help" ] || [ "$arg" = "-h" ] && _needs_root=false
done
if [ "$_needs_root" = true ] && [ "$(id -u)" -ne 0 ]; then
    echo "This needs root privileges. Re-running with sudo..."
    exec sudo bash "$0" "$@"
fi

py_version=$(python3 -c 'import sys; v=sys.version_info; print(f"{v.major}.{v.minor}")' 2>/dev/null || true)
if [ -z "$py_version" ] || [ "$(printf '%s\n' "3.11" "$py_version" | sort -V | head -1)" != "3.11" ]; then
    echo "Error: Python 3.11+ required (found: ${py_version:-none})"
    exit 1
fi

TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

if [ -n "$LOCAL_INSTALL" ]; then
    SRC="$LOCAL_INSTALL"
else
    ARCHIVE_URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.zip"
    echo "Downloading $REPO ($BRANCH)..."
    curl -fsSL --retry 3 --retry-delay 2 -o "$TMP_DIR/repo.zip" "$ARCHIVE_URL" || {
        echo "Error: Failed to download repository archive"; exit 1
    }
    unzip -q "$TMP_DIR/repo.zip" -d "$TMP_DIR" || { echo "Error: Failed to extract repository archive"; exit 1; }
    rm -f "$TMP_DIR/repo.zip"
    SRC="$TMP_DIR/$(echo "$REPO" | cut -d'/' -f2)-$(echo "$BRANCH" | tr '/' '-')"
fi
cp -r "$SRC/installer" "$TMP_DIR/installer"

export INSTALL_REPO="$REPO"
export INSTALL_BRANCH="$BRANCH"
export SWITCH_REPO_DIR="$SRC"
cd "$TMP_DIR"
python3 -m installer switch "${EXTRA_ARGS[@]}"
