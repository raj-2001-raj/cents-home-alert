#!/bin/bash
# Stops the background monitor and removes it from this Mac.
LABEL="com.cents-alert.monitor"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Library/Application Support/cents-alert"
echo "Monitor stopped and removed."
