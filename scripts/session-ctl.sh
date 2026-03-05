#!/bin/bash
# session-ctl.sh - CLI remote control for the Claude Session Tracker GUI
#
# Usage:
#   session-ctl.sh screenshot       # Capture GUI screenshot
#   session-ctl.sh refresh          # Force session rescan
#   session-ctl.sh state            # Get current state as JSON
#   session-ctl.sh resize WxH       # Resize window
#   session-ctl.sh move X,Y         # Move window

CMD_FILE="$HOME/.claude/.session-cmd"
RESULT_FILE="$HOME/.claude/.session-cmd-result"
TIMEOUT=10  # longer timeout since session scanning involves process lookups

if [ -z "$1" ]; then
    echo "Usage: session-ctl.sh <command>"
    echo ""
    echo "Commands:"
    echo "  screenshot        Capture GUI screenshot"
    echo "  refresh           Force session rescan"
    echo "  state             Get current state as JSON"
    echo "  resize WxH        Resize window (e.g. resize 400x200)"
    echo "  move X,Y          Move window (e.g. move 100,50)"
    exit 1
fi

# Clean up any previous result
rm -f "$RESULT_FILE"

# Write the command (pass all args)
echo "$*" > "$CMD_FILE"

# Wait for the result
elapsed=0
while [ ! -f "$RESULT_FILE" ] && [ "$elapsed" -lt "$TIMEOUT" ]; do
    sleep 0.2
    elapsed=$((elapsed + 1))
done

if [ -f "$RESULT_FILE" ]; then
    cat "$RESULT_FILE"
    rm -f "$RESULT_FILE"
else
    echo '{"ok": false, "error": "Timeout waiting for response. Is session-tracker.py running?"}'
    exit 1
fi
