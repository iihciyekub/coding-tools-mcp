#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Configure Coding Tools MCP macOS desktop-release signing secrets without storing credentials in Git.

Usage:
  scripts/setup-desktop-release-secrets.sh <developer-id.p12> <AuthKey_XXXXXXXXXX.p8> <apple-key-id> <apple-issuer-id>

Password input:
  - Set MAC_CSC_KEY_PASSWORD in the environment, or the script prompts securely.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ $# -lt 4 ]]; then
  usage >&2
  exit 2
fi

P12_PATH="$1"
APPLE_P8_PATH="$2"
APPLE_KEY_ID="$3"
APPLE_ISSUER_ID="$4"
shift 4

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

REPOSITORY="${CODING_TOOLS_RELEASE_REPOSITORY:-iihciyekub/coding-tools-mcp}"
ENVIRONMENT="${CODING_TOOLS_RELEASE_ENVIRONMENT:-production-release}"
MAC_CSC_NAME="${MAC_CSC_NAME:-Yongjian Li (2NLAH5MYH8)}"

command -v gh >/dev/null 2>&1 || { echo "GitHub CLI (gh) is required." >&2; exit 1; }
gh auth status >/dev/null
[[ -f "$P12_PATH" ]] || { echo "Developer ID .p12 does not exist: $P12_PATH" >&2; exit 1; }
[[ -f "$APPLE_P8_PATH" ]] || { echo "App Store Connect .p8 does not exist: $APPLE_P8_PATH" >&2; exit 1; }
[[ -n "$APPLE_KEY_ID" && -n "$APPLE_ISSUER_ID" ]] || { echo "Apple key ID and issuer ID are required." >&2; exit 1; }

P12_PASSWORD="${MAC_CSC_KEY_PASSWORD:-}"
if [[ -z "$P12_PASSWORD" ]]; then
  [[ -t 0 ]] || { echo "Set MAC_CSC_KEY_PASSWORD when running non-interactively." >&2; exit 1; }
  read -r -s -p "Developer ID .p12 password: " P12_PASSWORD
  echo
fi

echo "==> Ensure GitHub Environment exists"
gh api --method PUT "repos/$REPOSITORY/environments/$ENVIRONMENT" >/dev/null
gh variable set MAC_CSC_NAME --env "$ENVIRONMENT" --repo "$REPOSITORY" --body "$MAC_CSC_NAME"

set_secret_text() {
  local name="$1"
  local value="$2"
  printf '%s' "$value" | gh secret set "$name" --env "$ENVIRONMENT" --repo "$REPOSITORY"
}

set_secret_file_base64() {
  local name="$1"
  local file="$2"
  base64 < "$file" | tr -d '\r\n' | gh secret set "$name" --env "$ENVIRONMENT" --repo "$REPOSITORY"
}

echo "==> Upload macOS signing/notarization secrets"
set_secret_file_base64 MAC_CSC_P12_BASE64 "$P12_PATH"
set_secret_text MAC_CSC_KEY_PASSWORD "$P12_PASSWORD"
set_secret_file_base64 APPLE_API_KEY_P8_BASE64 "$APPLE_P8_PATH"
set_secret_text APPLE_API_KEY_ID "$APPLE_KEY_ID"
set_secret_text APPLE_API_ISSUER "$APPLE_ISSUER_ID"

unset P12_PASSWORD MAC_CSC_KEY_PASSWORD || true

echo "==> Configured secret names"
gh secret list --env "$ENVIRONMENT" --repo "$REPOSITORY"
gh variable list --env "$ENVIRONMENT" --repo "$REPOSITORY"
echo "Desktop release signing configuration is ready for $REPOSITORY ($ENVIRONMENT)."
echo "For an existing release tag, dispatch the canonical workflow with:"
echo "  gh workflow run desktop-release.yml -R $REPOSITORY --ref iiaide -f tag=desktop-v<version> -f mode=release"
