#!/usr/bin/env bash
# The agents' code on an agent computer: setup-runtime.sh UV_VERSION PYTHON COMPUTER KEY_PATH
# Unpacks ~/.nevia-agent/bundle.tgz (src/, agent.toml, requirements.txt, sent by `agent <m2p|acc> setup`),
# installs uv, then this Python and the requirements into ~/.nevia-agent/venv when they changed, and writes
# ~/.nevia-agent/bin/agent: the `agent` CLI here, signed in with the computer's key file (KEY_PATH, under the
# home; the key itself never goes in it).
set -euo pipefail
uv=${1:?usage: setup-runtime.sh UV_VERSION PYTHON COMPUTER KEY_PATH} python=$2 computer=$3 key=$4
home=$HOME/.nevia-agent
mkdir -p "$home/bin" "$home/build"
export UV_PYTHON_INSTALL_DIR=$home/python UV_CACHE_DIR=$home/cache

if [ "$("$home/bin/uv" --version 2>/dev/null | awk '{print $2}')" != "$uv" ]; then
  curl -LsSf --proto =https "https://astral.sh/uv/$uv/install.sh" |
    env UV_INSTALL_DIR="$home/bin" INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
fi

new=$home/new
rm -rf "$new" && mkdir -p "$new" && tar -xzf "$home/bundle.tgz" -C "$new"
if [ ! -x "$home/venv/bin/python" ] || ! cmp -s "$new/requirements.txt" "$home/requirements.txt"; then
  rm -rf "$home/venv"
  "$home/bin/uv" venv -q --python "$python" "$home/venv"
  "$home/bin/uv" pip install -q --python "$home/venv/bin/python" -r "$new/requirements.txt"
fi
rm -rf "$home/src"
mv "$new/src" "$new/agent.toml" "$new/requirements.txt" "$home/"
rm -rf "$new" "$home/bundle.tgz"

cat > "$home/bin/agent" <<EOF
#!/usr/bin/env bash
export PYTHONPATH=$home/src AGENT_COMPUTER=$computer NEVIA_KEY_FILE=$HOME/$key
exec $home/venv/bin/python -m core "\$@"
EOF
chmod 755 "$home/bin/agent"
"$home/bin/agent" --help >/dev/null
echo "runtime ready: $("$home/venv/bin/python" --version), uv $uv"
