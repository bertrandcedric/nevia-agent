# Shared by rc-lab.sh (on your machine) and remote/lab.sh (on the Nevia computer, where it is
# pushed next to it). Definitions only. TAG prefixes the messages.

say() { echo "[${TAG:-rc-lab}] $*"; }
# Timing: each step shows the time since START (the script's start) and how long the previous
# one took; done_ <what> says the last step's and the total.
START=${START:-$(date +%s)} STEP_START= STEP_NAME=
dur() { printf '%dm%02ds' $(($1 / 60)) $(($1 % 60)); }
step_done() { [ -z "$STEP_NAME" ] || say "⏱ $STEP_NAME: $(dur $(($(date +%s) - STEP_START)))"; STEP_NAME=; }
step() {
  step_done
  STEP_NAME=$* STEP_START=$(date +%s)
  printf '\n\033[1m[%s] ─── %s ─── +%s\033[0m\n' "${TAG:-rc-lab}" "$*" "$(dur $((STEP_START - START)))"
}
done_() { step_done; say "⏱ $1: $(dur $(($(date +%s) - START)))"; }
die() { echo "[${TAG:-rc-lab}] error: $*" >&2; exit 1; }

# load_env <file>: export its KEY=VALUE lines, unless already set (values never echoed).
load_env() {
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in ''|'#'*) continue ;; esac
    key=${line%%=*}; val=${line#*=}
    case $key in ''|*[!A-Za-z0-9_]*) die "$1: invalid variable name '$key'" ;; esac
    case $val in \"*\") val=${val#\"}; val=${val%\"} ;; \'*\') val=${val#\'}; val=${val%\'} ;; esac
    eval "[ -n \"\${$key+x}\" ]" || export "$key=$val"
  done < "$1"
}

# nv: the Nevia CLI, non-interactive. Retried when rate-limited (exit 6), its output kept until it
# succeeds and its stdin (--file -) replayed. exec passes straight through: its stdin is a script,
# its exit codes have their own meaning.
nv() {
  case $1 in exec) nevia --non-interactive "$@"; return ;; esac
  nv_in=; case " $* " in *" --file - "*) nv_in=$(cat) ;; esac
  nv_try=1
  while :; do
    nv_rc=0
    nv_out=$(printf '%s' "$nv_in" | nevia --non-interactive "$@") || nv_rc=$?
    if [ "$nv_rc" -eq 6 ] && [ "$nv_try" -lt 5 ]; then
      say "Nevia rate limit: retrying in 60 s ($nv_try/4)" >&2
      nv_try=$((nv_try + 1)); sleep 60; continue
    fi
    [ -z "$nv_out" ] || printf '%s\n' "$nv_out"
    return "$nv_rc"
  done
}

# status_json [pg] <name>: a computer (or a Postgres database) as JSON, nothing if it doesn't exist.
status_json() {
  if [ "$1" = pg ]; then set -- pg status "$2"; else set -- status "$1"; fi
  rc=0; out=$(nv --json "$@") || rc=$?
  case $rc in
    0) printf '%s' "$out" ;;
    3) ;;
    *) printf '%s\n' "$out" >&2; die "nevia $* failed (exit $rc)" ;;
  esac
}
json_field() { printf '%s' "$1" | jq -r --arg k "$2" 'first(.. | objects | select(has($k)) | .[$k]) // empty'; }

fetch() { curl -fsSL "$1" -o "$2"; }
# slug <name>: a Postgres identifier and directory name made from it (lowercase, [a-z0-9_]).
slug() { printf '%s' "$1" | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9_]/_/g; s/^_*//; s/_*$//'; }
running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
