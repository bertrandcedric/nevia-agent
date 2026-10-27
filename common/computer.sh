# The one Nevia computer of uc1, uc2 and uc3 (the agent, AGENT), its Nevia key and the runs going
# on it: sourced by rc-lab.sh, agent.sh and lab.sh (after their lib.sh: nv, say, die,
# status_json, json_field) and by agent/computer.sh. Definitions only.
#
# Two agents may run at the same time on it, so the key is handled once for all of them:
# - one principal, AGENT, and one key, in the secret NEVIA_AGENT_KEY, in the computer's env as
#   AGENT_NEVIA_KEY, its expiry (epoch) as AGENT_NEVIA_KEY_EXPIRES;
# - setup makes the key only when there is none: it never revokes the key a run is using;
#   agent_rotate_key renews it, and refuses while a run is going;
# - each process on the computer writes the key to a file of its own, deleted when it ends;
# - each run registers in ~/.busy/<uc>-<id> (its pid) on the computer: nothing puts the computer to
#   sleep, rotates the key or deletes the computer while one is there.
# Env: AGENT (default nevia-agent), AGENT_KEY_TTL (default 30d, 15m to 90d), NEVIA_WORKSPACE, and
# for the computer's env ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_AUTH_TOKEN (with
# ANTHROPIC_BASE_URL).

AGENT=${AGENT:-nevia-agent}
AGENT_KEY_TTL=${AGENT_KEY_TTL:-30d}
# A use case with a computer of its own (uc3) names its own secret.
AGENT_KEY_SECRET=${AGENT_KEY_SECRET:-NEVIA_AGENT_KEY}
AGENT_SECRETS="$AGENT_KEY_SECRET ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN"

agent_has_secret() { nv --json secrets ls | jq -e --arg n "$1" 'any(.secrets[]; .name == $n)' >/dev/null; }
agent_principal() { nv --json access ls | jq -r --arg n "$AGENT" 'first(.access[] | select(.principal.name == $n) | .principal.id) // empty'; }

# agent_busy: the runs going on the computer, of any use case (~/.busy/<uc>-<id>, a live pid each);
# nothing when it sleeps (nothing runs then) or does not exist.
agent_busy() {
  nv exec -n "$AGENT" -- sh -c 'for f in ~/.busy/*; do
      [ -e "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null && basename "$f"
    done; true' 2>/dev/null | tr '\n' ' ' | sed 's/ $//' || true
}

# agent_ttl_seconds: AGENT_KEY_TTL (e.g. 30d, 12h, 15m) in seconds.
agent_ttl_seconds() {
  case $AGENT_KEY_TTL in
    *[!0-9dhm]*|[!0-9]*|*[0-9]) die "AGENT_KEY_TTL: a number and d, h or m, e.g. 30d (got '$AGENT_KEY_TTL')" ;;
    *d) echo $((${AGENT_KEY_TTL%d} * 86400)) ;;
    *h) echo $((${AGENT_KEY_TTL%h} * 3600)) ;;
    *m) echo $((${AGENT_KEY_TTL%m} * 60)) ;;
  esac
}

# agent_new_key <principal or empty>: a new key (a new principal without one), into the secret;
# prints the -e flags that give it to the computer, one per line. The key itself never shows.
agent_new_key() {
  expires=$(($(date +%s) + $(agent_ttl_seconds)))
  if [ -z "$1" ]; then
    say "creating Nevia principal $AGENT (editor, key valid $AGENT_KEY_TTL)" >&2
    key=$(nv --yes access add --private-key --role editor --name "$AGENT" --description "Claude Code agents ($AGENT)" --expires-in "$AGENT_KEY_TTL" --out -)
  else
    say "renewing the key of $AGENT (valid $AGENT_KEY_TTL)" >&2
    key=$(nv --yes access keys rotate "$1" --expires-in "$AGENT_KEY_TTL" --out -)
  fi
  [ -n "$key" ] || die "no key returned for $AGENT"
  printf '%s\n' "$key" | nv --yes secrets "$AGENT_KEY_SECRET" --file - >/dev/null
  unset key
  agent_key_flags "$expires"
}

# agent_key_flags <expiry epoch or empty>: the -e flags that give the key in the secret to the
# computer, one per line.
agent_key_flags() {
  printf '%s\n' -e "AGENT_NEVIA_KEY={{secrets.$AGENT_KEY_SECRET}}"
  [ -z "$1" ] || printf '%s\n' -e "AGENT_NEVIA_KEY_EXPIRES=$1"
}

# agent_setup [<nevia new/set flags>...]: the principal and its key (only when there is none), Claude
# Code's credential, the computer (created, or its env and ports updated: what a use case needs
# besides, e.g. -p 443:3000). Safe while a run is going: it changes nothing it uses.
agent_setup() {
  # The flags captured first: a failure in $(...) stops the script then (set -e), not after.
  principal=$(agent_principal) flags=
  if [ -z "$principal" ]; then
    flags=$(agent_new_key "")
  elif ! agent_has_secret "$AGENT_KEY_SECRET"; then
    # A principal whose key is lost: a new key revokes the old one, which a run may be using.
    busy=$(agent_busy)
    [ -z "$busy" ] || die "no secret $AGENT_KEY_SECRET, and runs going on $AGENT ($busy): wait for them, then setup again"
    flags=$(agent_new_key "$principal")
  else
    say "Nevia key of $AGENT kept (renew it: agent/computer.sh rotate-key)"
    # Given to the computer all the same: one created since (e.g. after computer.sh destroy) has no
    # key otherwise. Its expiry from Nevia.
    expires=$(nv --json access ls | jq -r --arg n "$AGENT" '
      first(.access[] | select(.principal.name == $n) | .key.expires_at // empty) | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601')
    flags=$(agent_key_flags "$expires")
  fi
  set -f; old_ifs=$IFS; IFS='
'
  set -- "$@" $flags
  IFS=$old_ifs; set +f
  # Claude Code's credential, if .env has one: a workspace secret in the computer's env. With a
  # gateway (e.g. Aiven's AI gateway), ANTHROPIC_AUTH_TOKEN, next to ANTHROPIC_BASE_URL.
  [ -z "${ANTHROPIC_AUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_BASE_URL:-}" ] ||
    die "ANTHROPIC_AUTH_TOKEN without ANTHROPIC_BASE_URL in .env: the gateway's URL"
  for v in ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || continue
    printf '%s' "$val" | nv --yes secrets "$v" --file - >/dev/null
    set -- "$@" -e "$v={{secrets.$v}}"
  done
  unset val
  if [ -z "$(status_json "$AGENT")" ]; then
    say "creating computer $AGENT"
    nv new "$AGENT" "$@" --wait >/dev/null
  elif [ $# -gt 0 ]; then
    nv set "$AGENT" "$@" >/dev/null
  fi
  [ -n "${ANTHROPIC_API_KEY:-}${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_AUTH_TOKEN:-}" ] ||
    say "log Claude Code in once, if not done: nevia shell $AGENT, then claude and /login"
}

# agent_rotate_key: a new key for the computer, the old one revoked; never while a run is going (it
# would lose its access to Nevia midway).
agent_rotate_key() {
  principal=$(agent_principal)
  [ -n "$principal" ] || die "no principal $AGENT: setup first"
  [ -n "$(status_json "$AGENT")" ] || die "no computer $AGENT: setup first"
  busy=$(agent_busy)
  [ -z "$busy" ] || die "runs going on $AGENT ($busy): rotate once they are over"
  flags=$(agent_new_key "$principal")
  set -f; old_ifs=$IFS; IFS='
'
  set -- $flags
  IFS=$old_ifs; set +f
  nv set "$AGENT" "$@" >/dev/null
  say "key of $AGENT renewed, valid $AGENT_KEY_TTL"
}

# agent_sleep: the computer to sleep (compute released; storage, URL and data kept), unless a run
# of any use case is going on it. The next command wakes it.
agent_sleep() {
  case $(json_field "$(status_json "$AGENT")" state) in
    '') say "no computer $AGENT" ;;
    sleeping|suspended) say "$AGENT is already asleep" ;;
    *)
      busy=$(agent_busy)
      if [ -n "$busy" ]; then say "runs going on $AGENT ($busy): it stays awake"
      else say "putting $AGENT to sleep"; nv suspend "$AGENT" --wait >/dev/null && say "$AGENT is asleep"; fi ;;
  esac
}

# agent_destroy: the computer (every use case's files on it), its principal and key, Claude Code's
# secrets; never while a run is going. Each use case's own resources (databases...) are its own
# destroy's.
agent_destroy() {
  computer=$(status_json "$AGENT")
  [ -z "$computer" ] || { busy=$(agent_busy); [ -z "$busy" ] || die "runs going on $AGENT ($busy): stop them first"; }
  secrets=; for s in $AGENT_SECRETS; do ! agent_has_secret "$s" || secrets="$secrets $s"; done
  principal=$(agent_principal)
  [ -n "$computer$secrets$principal" ] || { say "nothing to delete in workspace $NEVIA_WORKSPACE"; return 0; }
  echo "In Nevia workspace $NEVIA_WORKSPACE, this deletes:"
  [ -z "$computer" ] || echo "  - computer $AGENT ($(json_field "$computer" url)): the files of uc1, uc2 and uc3 on it"
  for s in $secrets; do echo "  - secret $s"; done
  [ -z "$principal" ] || echo "  - Nevia principal $AGENT and its key"
  echo "Kept: each use case's databases and secrets (their own destroy)"
  if [ "${1:-}" != --yes ]; then
    [ -t 0 ] || die "destroy needs confirmation: --yes"
    printf 'This cannot be undone. Type "destroy": '; read -r answer; [ "$answer" = destroy ] || die "aborted"
  fi
  [ -z "$computer" ] || { say "deleting computer $AGENT"; nv --yes rm "$AGENT" --wait >/dev/null; }
  for s in $secrets; do say "deleting secret $s"; nv --yes secrets unset "$s" >/dev/null; done
  [ -z "$principal" ] || { say "deleting Nevia principal $AGENT"; nv --yes access rm "$principal" >/dev/null; }
}
