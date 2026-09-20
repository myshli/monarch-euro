#!/usr/bin/env bash
# Does Monarch accept our reused browser session from THIS machine?
#
# The session cookie travels with `cf_clearance`, which Cloudflare binds to the
# IP and User-Agent that earned it. That is the open question for running the
# sync anywhere other than the laptop the session came from, and one request
# answers it. Nothing is written; this only reads the signed-in user.
#
# Usage:  ./vps-auth-check.sh [path/to/.env]
# Or set MONARCH_COOKIE_HEADER and MONARCH_CSRF_TOKEN in the environment.

set -uo pipefail

ENV_FILE="${1:-$(dirname "$0")/../.env}"
if [[ -f "$ENV_FILE" ]]; then
  # Read the two values without exporting the whole file.
  MONARCH_COOKIE_HEADER="${MONARCH_COOKIE_HEADER:-$(grep -m1 '^MONARCH_COOKIE_HEADER=' "$ENV_FILE" | cut -d= -f2-)}"
  MONARCH_CSRF_TOKEN="${MONARCH_CSRF_TOKEN:-$(grep -m1 '^MONARCH_CSRF_TOKEN=' "$ENV_FILE" | cut -d= -f2-)}"
fi

if [[ -z "${MONARCH_COOKIE_HEADER:-}" || -z "${MONARCH_CSRF_TOKEN:-}" ]]; then
  echo "FAIL  MONARCH_COOKIE_HEADER / MONARCH_CSRF_TOKEN not found."
  echo "      Pass an .env path, or export them."
  exit 2
fi

UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT

echo "Public IP of this machine: $(curl -s --max-time 10 https://api.ipify.org || echo unknown)"
echo "Querying Monarch as the signed-in user..."

CODE="$(curl -s -o "$TMP" -w '%{http_code}' --max-time 30 \
  -X POST 'https://api.monarch.com/graphql' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'Client-Platform: web' \
  -H 'monarch-client: monarch-core-web-app-graphql' \
  -H 'monarch-client-version: v1.0.4742' \
  -H "x-csrftoken: ${MONARCH_CSRF_TOKEN}" \
  -H "Cookie: ${MONARCH_COOKIE_HEADER}" \
  -H "User-Agent: ${UA}" \
  -H 'Origin: https://app.monarch.com' \
  -H 'Referer: https://app.monarch.com/' \
  -d '{"query":"{ me { id email } }"}')"

BODY="$(head -c 600 "$TMP")"
echo "HTTP $CODE"

case "$CODE" in
  200)
    if grep -q '"me"' "$TMP" && ! grep -q '"errors"' "$TMP"; then
      echo "PASS  The session works from this machine. The VPS can run the sync."
      exit 0
    fi
    echo "FAIL  200 but no user returned - the session is probably expired."
    echo "      $BODY"
    exit 1 ;;
  403)
    if grep -qiE 'cloudflare|just a moment|cf-|attention required' "$TMP"; then
      echo "FAIL  Cloudflare blocked this machine."
      echo "      cf_clearance is bound to the IP and User-Agent that earned it,"
      echo "      so a session captured on your laptop will not travel to a VPS."
      echo "      Use 'monarch-euro export' plus a manual upload, or run the sync"
      echo "      on the machine the session came from."
    else
      echo "FAIL  Monarch rejected the session (403). It may have expired."
      echo "      $BODY"
    fi
    exit 1 ;;
  401)
    echo "FAIL  Session expired or invalid. Re-copy the cookies from a browser."
    exit 1 ;;
  429)
    echo "FAIL  Rate limited. Wait before retrying; repeated attempts extend it."
    exit 1 ;;
  *)
    echo "FAIL  Unexpected response."
    echo "      $BODY"
    exit 1 ;;
esac
