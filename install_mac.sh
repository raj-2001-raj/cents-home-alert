#!/bin/bash
# Runs the CENT@HOME monitor in the background on this Mac, every 60 seconds,
# whenever you're logged in and the Mac is awake. Re-run after editing config.json.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
# Not on the Desktop: macOS blocks background jobs from reading ~/Desktop.
DEST="$HOME/Library/Application Support/cents-alert"
LABEL="com.cents-alert.monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL="${1:-60}"

if [ ! -f "$SRC/config.json" ]; then
  echo "Missing $SRC/config.json - copy config.example.json to config.json and set ntfy_topic first." >&2
  exit 1
fi

mkdir -p "$DEST" "$HOME/Library/LaunchAgents"
cp "$SRC/cents_monitor.py" "$SRC/config.json" "$DEST/"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$DEST/cents_monitor.py</string>
    <string>--log</string>
    <string>$DEST/monitor.log</string>
  </array>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DEST/launchd.log</string>
  <key>StandardErrorPath</key><string>$DEST/launchd.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed. Checking every ${INTERVAL}s."
echo "  log:       tail -f \"$DEST/monitor.log\""
echo "  remove:    \"$SRC/uninstall_mac.sh\""
