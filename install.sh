#!/usr/bin/env bash
# Install quota-dash into the Hermes Desktop plugin door.
#
#   ./install.sh
#
# Copies plugin.js + probe.py into $HERMES_HOME/desktop-plugins/quota-dash/.
# The desktop app watches that folder and loads/hot-reloads the plugin within
# seconds. If the pane does not appear, run Cmd+K -> "Reload desktop plugins".

set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${HERMES_HOME:-$HOME/.hermes}/desktop-plugins/quota-dash"

mkdir -p "$DEST"
cp -f "$SRC/plugin.js" "$DEST/plugin.js"
cp -f "$SRC/probe.py" "$DEST/probe.py"
chmod +x "$DEST/probe.py"

echo "installed quota-dash -> $DEST"
echo
echo "check the backend can run the probe:"
echo "  $HOME/.hermes/hermes-agent/venv/bin/python $DEST/probe.py"
