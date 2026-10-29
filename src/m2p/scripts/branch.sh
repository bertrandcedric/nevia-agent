#!/usr/bin/env bash
# Create m2p-tryN, a fresh branch of the target database, and print the path of its URI file.
set -euo pipefail
loop=$(cd "$(dirname "$0")/.." && pwd)
source "$loop/tools/env.sh"
n=${1:?usage: branch.sh N}
[[ $n =~ ^[0-9]+$ ]] || { echo "N must be a number" >&2; exit 2; }
name="m2p-try$n"
nevia pg branch "$M2P_PG" -b "$name" --wait --timeout 15m >/dev/null
umask 077
mkdir -p "$loop/tries"
nevia pg connect "$name" --uri > "$loop/tries/$name.uri"
echo "$loop/tries/$name.uri"
