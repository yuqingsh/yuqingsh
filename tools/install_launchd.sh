#!/bin/sh
# Install (or update) the launchd collector job from the plist template.
#
#   sh tools/install_launchd.sh [shadow-repo-dir]
#
# Environment:
#   CODEX_REMOTE_HOSTS   comma-separated ssh hosts for remote Codex scans
#                        (baked into the installed plist, never committed)
#
# Default shadow-repo-dir: ~/.local/share/token-usage-repo
set -eu

TEMPLATE="$(cd "$(dirname "$0")" && pwd)/com.github.yuqingsh.tokenusage.plist"
REPO_DIR="${1:-$HOME/.local/share/token-usage-repo}"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/com.github.yuqingsh.tokenusage.plist"
LABEL="com.github.yuqingsh.tokenusage"

if [ ! -d "$REPO_DIR/.git" ]; then
    echo "[install] cloning shadow repo into $REPO_DIR"
    git clone https://github.com/yuqingsh/yuqingsh.git "$REPO_DIR"
fi

mkdir -p "$TARGET_DIR"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
    -e "s|__CODEX_REMOTE_HOSTS__|${CODEX_REMOTE_HOSTS:-}|g" \
    "$TEMPLATE" > "$TARGET"

if launchctl list "$LABEL" >/dev/null 2>&1; then
    launchctl unload "$TARGET"
fi
launchctl load "$TARGET"
echo "[install] loaded $TARGET (repo: $REPO_DIR, hosts: ${CODEX_REMOTE_HOSTS:-none})"
