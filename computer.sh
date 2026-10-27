#!/bin/sh
# The one Nevia computer of uc1, uc2 and uc3 (AGENT in .env, nevia-agent by default): each use
# case's setup makes it; this manages it. Config: .env only (see .env.example).
#
# Usage: ./computer.sh setup               the computer, its Nevia key (only if it has none), Claude
#                                          Code's credential
#        ./computer.sh status              the computer, its key's expiry, the runs going on it
#        ./computer.sh rotate-key          a new Nevia key, the old one revoked (refused while a
#                                          run is going)
#        ./computer.sh sleep               the computer to sleep (not while a run is going)
#        ./computer.sh destroy [--yes]     the computer (every use case's files on it), its
#                                          principal and key, Claude Code's secrets
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/common/lib.sh"
TAG=computer
usage() { sed -n '2,/^[^#]/ s/^# \{0,1\}//p' "$0"; }

CMD=${1:-}
case $CMD in
  setup|status|rotate-key|sleep|destroy) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 1 ;;
esac

# .env alone decides: the shell's values are dropped first.
unset NEVIA_WORKSPACE AGENT AGENT_KEY_TTL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
load_env "$DIR/.env"
for c in nevia jq; do command -v $c >/dev/null || die "$c not found"; done
[ -n "${NEVIA_WORKSPACE:-}" ] || die "set NEVIA_WORKSPACE in .env"
export NEVIA_WORKSPACE
. "$DIR/common/computer.sh"

status() {
  nv status "$AGENT" || return 0
  exp=$(nv exec -n "$AGENT" -- sh -c 'echo "${AGENT_NEVIA_KEY_EXPIRES:-}"' 2>/dev/null || true)
  case $exp in
    ''|*[!0-9]*) say "key expiry: unknown (the computer sleeps, or its key predates this script)" ;;
    *) say "key expires in $(( (exp - $(date +%s)) / 86400 )) days ($(date -u -r "$exp" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || date -u -d "@$exp" '+%Y-%m-%d %H:%M UTC'))" ;;
  esac
  busy=$(agent_busy)
  say "runs going: ${busy:-none}"
}

case $CMD in
  setup)      agent_setup; say "ready: $AGENT ($(json_field "$(status_json "$AGENT")" url))" ;;
  status)     status ;;
  rotate-key) agent_rotate_key ;;
  sleep)      agent_sleep ;;
  destroy)    agent_destroy "${2:-}" ;;
esac
