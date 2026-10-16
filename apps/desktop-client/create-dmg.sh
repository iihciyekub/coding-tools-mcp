#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION="${CODING_TOOLS_MCP_DESKTOP_VERSION:-$(node -e 'const fs=require("fs"); const p=process.argv[1]; process.stdout.write(JSON.parse(fs.readFileSync(p,"utf8")).version)' "$SCRIPT_DIR/src-tauri/tauri.conf.json")}"
case "$(uname -m)" in
  arm64) BUNDLE_ARCH="aarch64" ;;
  x86_64) BUNDLE_ARCH="x64" ;;
  *) BUNDLE_ARCH="$(uname -m)" ;;
esac

APP_PATH="${1:-src-tauri/target/release/bundle/macos/Coding Tools MCP.app}"
OUTPUT_PATH="${2:-src-tauri/target/release/bundle/dmg/Coding Tools MCP_${VERSION}_${BUNDLE_ARCH}.dmg}"
VOLNAME="Coding Tools MCP"
DMG_ICON_SIZE="112"

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
    if ! hdiutil detach "$MOUNT_DEVICE" -force >/dev/null 2>&1; then
      echo "Unable to detach $MOUNT_DEVICE; leaving staging directory at $TMP_DIR" >&2
      return
    fi
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

detach_image() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if hdiutil detach "$MOUNT_DEVICE" >/dev/null; then
      MOUNT_DEVICE=""
      return 0
    fi
    if [[ "$attempt" -lt 5 ]]; then
      echo "DMG volume is busy; retrying detach ($attempt/5)" >&2
      sleep "$attempt"
    fi
  done
  # Only force the temporary image attached by this script, after metadata
  # has been synced and normal detach has had a bounded chance to succeed.
  echo "Normal detach exhausted; forcing detach of $MOUNT_DEVICE" >&2
  if hdiutil detach "$MOUNT_DEVICE" -force >/dev/null; then
    MOUNT_DEVICE=""
    return 0
  fi
  echo "DMG volume could not be detached; refusing to convert a mounted image" >&2
  return 1
}

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
    set icon size of theViewOptions to $DMG_ICON_SIZE
    set text size of theViewOptions to 13
    set position of item "Coding Tools MCP.app" to {165, 185}
    set position of item "Applications" to {435, 185}
    close
    open
    update without registering applications
    delay 2
    close
  end tell
end tell
APPLESCRIPT

sync
detach_image

rm -f "$OUTPUT_PATH"
hdiutil convert \
  "$RW_DMG" \
  -format UDZO \
  -imagekey zlib-level=9 \
  -o "$OUTPUT_PATH" >/dev/null

echo "$OUTPUT_PATH"
