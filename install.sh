#!/usr/bin/env bash
# taskman installer — run from the directory containing taskman.py
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/taskman"
SERVICE_DIR="$HOME/.config/systemd/user"

echo "==> Installing taskman..."

# 1. Python dependencies
echo "    Installing Python deps..."
pip install --quiet --break-system-packages textual 2>/dev/null \
  || pip install --quiet --user textual 2>/dev/null \
  || { echo "WARNING: pip install failed — install textual manually"; }

# 2. Copy script
mkdir -p "$BIN_DIR"
cp "$SCRIPT_DIR/taskman.py" "$BIN_DIR/taskman"
chmod +x "$BIN_DIR/taskman"
echo "    Installed: $BIN_DIR/taskman"

# 3. Make sure ~/.local/bin is in PATH
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
  SHELL_RC="$HOME/.bashrc"
  [[ "$SHELL" == *zsh* ]] && SHELL_RC="$HOME/.zshrc"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
  echo "    Added ~/.local/bin to PATH in $SHELL_RC — restart your shell or run: source $SHELL_RC"
fi

# 4. Systemd user service (persistent daemon — notifications even when TUI is closed)
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_DIR/taskman-daemon.service" <<EOF
[Unit]
Description=taskman notification daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=$BIN_DIR/taskman daemon
Restart=on-failure
RestartSec=10s
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%i/bus

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now taskman-daemon.service 2>/dev/null \
  && echo "    Systemd service enabled: taskman-daemon.service" \
  || echo "    (Could not enable systemd service — run 'taskman daemon &' manually if needed)"

# 5. Desktop autostart entry for the widget (starts with your session)
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/taskman-widget.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=taskman widget
Exec=$BIN_DIR/taskman widget
Comment=taskman floating task widget
X-GNOME-Autostart-enabled=true
EOF
echo "    Widget autostart entry created: $AUTOSTART_DIR/taskman-widget.desktop"

echo ""
echo "✓ Done!  Usage:"
echo ""
echo "  taskman widget                   # floating desktop widget (auto-starts next login)"
echo "  taskman                          # open TUI"
echo "  taskman add 'Buy milk'           # quick add to Inbox"
echo "  taskman add 'Sprint review' --due '2026-09-12 15:00' --project Work --priority high"
echo "  taskman add 'Water plants' --recur weekly --due '2026-09-15 08:00'"
echo "  taskman list                     # open tasks"
echo "  taskman list --today             # due today"
echo "  taskman list --overdue           # overdue only"
echo "  taskman done <id>                # toggle done"
echo "  taskman rm <id>                  # delete"
echo "  taskman ping <id>                # fire a desktop notification now"
echo "  taskman projects                 # list projects"
echo ""
echo "  Desktop notifications fire automatically 15 min before due time,"
echo "  on overdue, and at 9 am daily (via the background daemon)."
