#!/bin/bash
set -euo pipefail

APP_PATH="${1:-src-tauri/target/release/bundle/macos/Coding Tools MCP.app}"
OUTPUT_PATH="${2:-src-tauri/target/release/bundle/dmg/Coding Tools MCP_0.3.19_aarch64.dmg}"
VOLNAME="Coding Tools MCP"

if [[ ! -d "$APP_PATH" ]]; then
  echo "App bundle not found: $APP_PATH" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_PATH")"

TMP_DIR="$(mktemp -d /tmp/coding-tools-mcp-dmg.XXXXXX)"
RW_DMG="$TMP_DIR/layout.dmg"
MOUNT_POINT="$TMP_DIR/mount"
MOUNT_DEVICE=""

cleanup() {
  if [[ -n "$MOUNT_DEVICE" ]]; then
    hdiutil detach "$MOUNT_DEVICE" -force >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$MOUNT_POINT"

# A small writable image is enough for the lightweight app plus Finder metadata.
hdiutil create \
  -size 40m \
  -fs HFS+ \
  -volname "$VOLNAME" \
  -ov \
  "$RW_DMG" >/dev/null

ATTACH_OUTPUT="$(hdiutil attach \
  -readwrite \
  -noverify \
  -noautoopen \
  -mountpoint "$MOUNT_POINT" \
  "$RW_DMG")"
MOUNT_DEVICE="$(printf '%s\n' "$ATTACH_OUTPUT" | awk '$1 ~ /^\/dev\/disk[0-9]+$/ { print $1; exit }')"

ditto "$APP_PATH" "$MOUNT_POINT/Coding Tools MCP.app"
ln -s /Applications "$MOUNT_POINT/Applications"

# Create the familiar macOS drag-to-Applications Finder layout.
osascript <<APPLESCRIPT
tell application "Finder"
  set dmgFolder to POSIX file "$MOUNT_POINT" as alias
  set dmgDisk to disk of dmgFolder
  tell dmgDisk
    open
    set current view of container window to icon view
    set bounds of container window to {120, 120, 720, 470}
    set theViewOptions to icon view options of container window
    set arrangement of theViewOptions to not arranged
    set icon size of theViewOptions to 112
    set text size of theViewOptions to 13
    set position of item "Coding Tools MCP.app" to {165, 185}
    set position of item "Applications" to {435, 185}
    close
    open
    update without registering applications
    delay 2
  end tell
end tell
APPLESCRIPT

sync
hdiutil detach "$MOUNT_DEVICE" >/dev/null
MOUNT_DEVICE=""

rm -f "$OUTPUT_PATH"
hdiutil convert \
  "$RW_DMG" \
  -format UDZO \
  -imagekey zlib-level=9 \
  -o "$OUTPUT_PATH" >/dev/null

echo "$OUTPUT_PATH"
