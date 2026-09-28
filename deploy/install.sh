#!/usr/bin/env bash
# Install the monitor on a Linux server: build the image, create the state volume, install the
# timers. Idempotent - run it again after a change and it updates what changed.
#
#   sudo ./deploy/install.sh              # build, install timers, leave them stopped
#   sudo ./deploy/install.sh --enable     # and start them
#
# It deliberately does not create .env or ask for a credential: secrets are the operator's to place,
# and a script that prompts for them tends to end up with them in a shell history.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/az-macro-banking}"
ENABLE=0
[ "${1:-}" = "--enable" ] && ENABLE=1

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: the timers install into /etc/systemd/system"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed"

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"

if [ "$SRC" != "$REPO_DIR" ]; then
  say "copying the repository to $REPO_DIR"
  mkdir -p "$REPO_DIR"
  # data/ and outputs/ are never copied: the volume is the state, and overwriting it from a working
  # copy is how a deployment loses its history.
  tar -C "$SRC" --exclude=./data --exclude=./outputs --exclude=./.git -cf - . | tar -C "$REPO_DIR" -xf -
fi
cd "$REPO_DIR/deploy"

if [ ! -f "$REPO_DIR/.env" ]; then
  say "creating .env from the example (no credentials are set; delivery stays off)"
  cp env.example "$REPO_DIR/.env"
  chmod 600 "$REPO_DIR/.env"
fi

say "building the image"
docker compose build

say "checking that the configuration loads and the timers match config/schedule.yaml"
docker compose run --rm cli schedule check || die "the timers and config/schedule.yaml disagree; fix before installing"

say "installing the systemd units"
install -m 0644 systemd/azmonitor-*.service systemd/azmonitor-*.timer /etc/systemd/system/
systemctl daemon-reload

if [ "$ENABLE" -eq 1 ]; then
  say "enabling the timers"
  systemctl enable --now azmonitor-source-check.timer azmonitor-weekly.timer azmonitor-monitor.timer
  systemctl list-timers 'azmonitor-*' --no-pager
else
  cat <<'MSG'

The timers are installed but not started. Nothing runs yet.

Before starting them:
  1. put the dataset in place, either by restoring a backup into the volume or with
       docker compose run --rm cli backfill --start 2020-01      (about half an hour)
  2. check what the first run would do, without doing it:
       docker compose run --rm cli run-task --task source-check --dry-run
  3. start them:
       sudo systemctl enable --now azmonitor-source-check.timer azmonitor-weekly.timer azmonitor-monitor.timer

Delivery stays off until config/delivery.yaml sets enabled: true and .env carries the Graph
credentials. Until then every run records what it would have sent and why it did not.
MSG
fi
