#!/usr/bin/env bash
# Deploy a built report as Cloudflare Workers static assets, private behind Cloudflare Access.
#   publish.sh <dist-dir> <session-id> [--production]
# Each session uploads a Worker version with preview alias <session-id> → its own preview URL
#   https://<alias>-<worker>.<subdomain>.workers.dev
# --production also makes it the live "latest" at https://<worker>.<subdomain>.workers.dev
#
# Config: SESSION_VIZ_SUBDOMAIN (your account's workers.dev subdomain) — env, or a line
#   SUBDOMAIN=<name> in ~/.config/session-viz/config. SESSION_VIZ_WORKER (default session-viz).
#   WRANGLER (default: wrangler on PATH, else newest nvm install, else npx wrangler).
set -euo pipefail

die() { echo "publish.sh: $*" >&2; exit 1; }

[[ $# -ge 2 ]] || die "usage: publish.sh <dist-dir> <session-id> [--production]"
DIST=$1 SID=$2 PROD=${3:-}
WORKER=${SESSION_VIZ_WORKER:-session-viz}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}/session-viz/config
SUB=${SESSION_VIZ_SUBDOMAIN:-$( [[ -f $CONF ]] && sed -n 's/^SUBDOMAIN=//p' "$CONF" | tail -1 || true)}
[[ -n $SUB ]] || die "workers.dev subdomain unknown. Find it under Workers & Pages → Overview (right sidebar,
     'Subdomain'), then: mkdir -p ${CONF%/*} && echo SUBDOMAIN=<name> >> $CONF"

[[ -f "$DIST/index.html" ]] || die "$DIST/index.html not found; run build.py first"
if grep -Eq '<(script|link|img)[^>]+(src|href)="https?://' "$DIST/index.html"; then
  die "$DIST/index.html references external URLs; refusing to publish (report must be self-contained)"
fi

if [[ -n "${WRANGLER:-}" ]]; then
  read -r -a WR <<<"$WRANGLER"
elif command -v wrangler >/dev/null; then
  WR=(wrangler)
elif NVM_WR=$(ls -1d "$HOME"/.nvm/versions/node/*/bin/wrangler 2>/dev/null | sort -V | tail -1) && [[ -n $NVM_WR ]]; then
  # nvm installs aren't on PATH in non-interactive shells (e.g. when run by the skill); its node must be too
  PATH="$(dirname "$NVM_WR"):$PATH"
  WR=("$NVM_WR")
elif command -v npx >/dev/null; then
  WR=(npx --yes wrangler)
else
  die "wrangler not found. Install: npm i -g wrangler"
fi

if ! WHO=$("${WR[@]}" whoami 2>&1) || grep -qi "not authenticated\|not logged in" <<<"$WHO"; then
  die "wrangler whoami failed — not logged in? Run: ${WR[*]} login   (then re-run this script). Output:
$(tail -5 <<<"$WHO")"
fi

# Preview alias = DNS label "<alias>-<worker>" (≤ 63 chars): lowercase alnum + dashes
MAXA=$(( 63 - ${#WORKER} - 1 ))
ALIAS=$(tr '[:upper:]' '[:lower:]' <<<"$SID" | tr -c 'a-z0-9\n' '-' | cut -c1-"$MAXA" | sed 's/^-*//; s/-*$//')
[[ -n $ALIAS ]] || die "session id '$SID' yields an empty alias"
HOST="$WORKER.$SUB.workers.dev"

# Access gate, BEFORE uploading anything. Protected hosts 30x-redirect to <team>.cloudflareaccess.com.
# The random probe host checks the preview wildcard (*-<worker>.<sub>.workers.dev) too. This also means a
# missing subdomain stops us here, so wrangler never gets the chance to auto-register one.
behind_access() { curl -s -o /dev/null -m 15 -w '%{http_code} %{redirect_url}' "$1" | grep -Eq '^30[0-9] .*cloudflareaccess\.com'; }
if [[ -z "${SESSION_VIZ_SKIP_ACCESS_CHECK:-}" ]]; then
  PROBE="probe$RANDOM-$WORKER.$SUB.workers.dev"
  for h in "$HOST" "$PROBE"; do
    behind_access "https://$h" || die "https://$h is not behind Cloudflare Access; refusing to publish.
     Protect both $HOST and *-$HOST with an Access application first (README.md § Hosting)."
  done
fi

# Throwaway config so nothing is written into the repo and wrangler's project autodetection stays out of it
TMP=$(mktemp -d "${TMPDIR:-/tmp}/session-viz-deploy.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
cat >"$TMP/wrangler.jsonc" <<EOF
{"name": "$WORKER", "compatibility_date": "2026-10-01", "workers_dev": true, "preview_urls": true,
 "assets": {"directory": "$(cd "$DIST" && pwd)"}}
EOF

if [[ $PROD == --production ]]; then
  OUT=$(cd "$TMP" && "${WR[@]}" deploy --config "$TMP/wrangler.jsonc" --message "session $SID" 2>&1) \
    || { echo "$OUT" >&2; die "deploy failed"; }
  URLS=("https://$HOST")
else
  OUT=$(cd "$TMP" && "${WR[@]}" versions upload --config "$TMP/wrangler.jsonc" --preview-alias "$ALIAS" \
          --message "session $SID" 2>&1) || { echo "$OUT" >&2; die "upload failed"; }
  URLS=()
  while read -r u; do URLS+=("$u"); done < <(grep -Eo 'https://[a-z0-9.-]+\.workers\.dev' <<<"$OUT" | sort -u)
  [[ ${#URLS[@]} -gt 0 ]] || { echo "$OUT" >&2; die "uploaded, but no preview URL in wrangler output (preview URLs disabled?)"; }
fi
echo "$OUT" >&2
printf 'url: %s\n' "${URLS[@]}"

if [[ -z "${SESSION_VIZ_SKIP_ACCESS_CHECK:-}" ]]; then
  sleep 5  # new preview hosts can take a moment to resolve
  for u in "${URLS[@]}"; do
    behind_access "$u" || { echo "WARNING: $u is NOT behind Cloudflare Access. Fix the Access application" \
      "now (README.md § Hosting), or delete this version in the dashboard." >&2; exit 3; }
  done
  echo "verified: every URL redirects to the Cloudflare Access login"
fi
