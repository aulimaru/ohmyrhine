#!/usr/bin/env bash
# Copy the project to the gateway host, install dependencies and (re)start the gateway.
# Runtime data (data/), the virtualenv and the token never leave their machines.
#
# usage: deploy/deploy.sh [user@]host [--no-restart]
set -euo pipefail

target_host=
RESTART=yes
for arg in "$@"; do
    case "$arg" in
        --no-restart) RESTART=no ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) if [[ -n $target_host || -z $arg ]]; then
               echo "usage: deploy/deploy.sh [user@]host [--no-restart]" >&2; exit 2
           fi
           target_host=$arg ;;
    esac
done
if [[ -z $target_host ]]; then
    echo "usage: deploy/deploy.sh [user@]host [--no-restart]" >&2
    exit 2
fi
DEST=servers/ohMyRhine
cd "$(dirname "$0")/.."

ssh "$target_host" mkdir -p "$DEST"
# Sync the application only. The host owns its config.yaml, credentials and runtime data.
rsync -az --delete --exclude .git --exclude-from=.gitignore \
    src characters deploy docs README.md LICENSE config.example.yaml pyproject.toml uv.lock .python-version \
    "$target_host:$DEST/"

ssh "$target_host" bash -s -- "$DEST" "$RESTART" <<'REMOTE'
set -euo pipefail
dest=$1 restart=$2
cd ~/"$dest"
if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
fi
uv sync --frozen --no-dev
# The connector reads this token locally; created once, mode 600, never printed.
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
install -d -m 700 "$config_home/ohmyrhine"
token_file="$config_home/ohmyrhine/gateway.token"
if [ ! -f "$token_file" ]; then
    (umask 077 && set -C && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$token_file")
fi
install -D -m 644 deploy/ohmyrhine-gateway.service "$config_home/systemd/user/ohmyrhine-gateway.service"
systemctl --user daemon-reload
systemctl --user enable ohmyrhine-gateway >/dev/null
# A user service only keeps running after logout (and starts at boot) with lingering on.
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q '=yes'; then
    echo "warning: lingering is off for $USER; run 'loginctl enable-linger $USER' so the gateway survives logout" >&2
fi
if [ "$restart" = yes ]; then
    systemctl --user restart ohmyrhine-gateway
    sleep 2
    if ! systemctl --user is-active --quiet ohmyrhine-gateway; then
        journalctl --user -u ohmyrhine-gateway -n 30 --no-pager
        exit 1
    fi
    systemctl --user --no-pager --lines=0 status ohmyrhine-gateway
fi
REMOTE
