#!/usr/bin/env bash
# The agents' code on an agent computer: setup-runtime.sh UV_VERSION PYTHON NEVIA_VERSION WORKSPACE COMPUTER KEY_PATH
# Unpacks ~/.nevia-agent/bundle.tgz (src/, prompts/, agent.toml, requirements.txt, sent by `agent <m2p|acc> setup`),
# installs uv, then this Python and the requirements into ~/.nevia-agent/venv when they changed, the nevia CLI
# into ~/.local/bin, signed in with the computer's key file (KEY_PATH, under the home) in WORKSPACE, and writes
# ~/.nevia-agent/bin/agent: the `agent` CLI here, with the same key file (the key itself never goes in it).
set -euo pipefail
uv=${1:?usage: setup-runtime.sh UV_VERSION PYTHON NEVIA_VERSION WORKSPACE COMPUTER KEY_PATH} python=$2
nevia=$3 workspace=$4 computer=$5 key=$6
home=$HOME/.nevia-agent
mkdir -p "$home/bin" "$home/build" "$HOME/.local/bin"
export UV_PYTHON_INSTALL_DIR=$home/python UV_CACHE_DIR=$home/cache

cli=$HOME/.local/bin/nevia
if [ "$("$cli" --version 2>/dev/null | awk '{print $2}')" != "$nevia" ]; then
  curl -fsSL --proto =https https://get.nevia.cloud | NEVIA_VERSION=$nevia NEVIA_INSTALL_DIR=$HOME/.local/bin sh >/dev/null
fi
# Nevia reserves NEVIA_* in a computer's env, so the selection goes in the CLI's own config.
"$cli" auth use-key-file "$HOME/$key" >/dev/null
"$cli" workspace use "$workspace" >/dev/null
"$cli" auth access-token >/dev/null

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
rm -rf "$home/src" "$home/prompts"
mv "$new/src" "$new/prompts" "$new/agent.toml" "$new/requirements.txt" "$home/"
rm -rf "$new" "$home/bundle.tgz"

cat > "$home/bin/agent" <<EOF
#!/usr/bin/env bash
export PYTHONPATH=$home/src AGENT_COMPUTER=$computer NEVIA_KEY_FILE=$HOME/$key PATH=$HOME/.local/bin:\$PATH
exec $home/venv/bin/python -m core "\$@"
EOF
chmod 755 "$home/bin/agent"
"$home/bin/agent" --help >/dev/null
echo "runtime ready: $("$home/venv/bin/python" --version), uv $uv, $("$cli" --version), signed in"
