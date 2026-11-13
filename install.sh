#!/bin/sh
# Install pipequi for the current user: a command in ~/.local/bin and a menu entry.
set -e
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
chmod +x "$here/pipequi.py"
ln -sf "$here/pipequi.py" "$HOME/.local/bin/pipequi"
cp "$here/pipequi.desktop" "$HOME/.local/share/applications/pipequi.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Installed. Run 'pipequi' or open it from the menu."
