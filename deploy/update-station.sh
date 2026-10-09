#!/usr/bin/env bash
# Push this checkout's COMMITTED code to a listening station and restart it, safely:
# stage it beside the running copy, check every Python file parses with the station's own Python, then
# swap it in (lorakeet.toml and venv are never touched) and restart. A broken file never reaches the
# running copy. Reinstalls Python packages only if requirements.txt changed.
#   deploy/update-station.sh lorakeet@my-station
set -euo pipefail
TARGET=${1:?usage: deploy/update-station.sh user@station-host}
cd "$(git rev-parse --show-toplevel)"
git diff --quiet HEAD -- '*.py' 'static/*' || echo "note: uncommitted changes are NOT sent (only HEAD $(git rev-parse --short HEAD))"
tmp="$(mktemp -d)/lorakeet.tar"
out="$tmp"; command -v cygpath >/dev/null && out="$(cygpath -w "$tmp")"   # Git Bash on Windows
git -c core.autocrlf=false archive --format=tar -o "$out" HEAD   # no CRLF conversion: stations are Linux
scp -q "$tmp" "$TARGET:lorakeet-update.tar"
ssh "$TARGET" 'set -e
  rm -rf ~/stage && mkdir ~/stage && tar -x -f ~/lorakeet-update.tar -C ~/stage && rm ~/lorakeet-update.tar
  for f in ~/stage/*.py; do ~/lorakeet/venv/bin/python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f" \
    || { echo "PARSE ERROR in $f: not deployed"; exit 1; }; done
  cmp -s ~/stage/requirements.txt ~/lorakeet/requirements.txt && pip=0 || pip=1
  cp -r ~/stage/. ~/lorakeet/ && rm -rf ~/stage
  [ "$pip" = 1 ] && ~/lorakeet/venv/bin/pip install -q --prefer-binary -r ~/lorakeet/requirements.txt
  sudo -n systemctl restart lorakeet && sleep 25
  echo "service: $(systemctl is-active lorakeet)"
  curl -s localhost:5190/api/sync; echo'
