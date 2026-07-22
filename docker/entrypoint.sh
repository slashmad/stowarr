#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-002}"

case "$PUID:$PGID" in
  *[!0-9:]*|:*|*:) echo "PUID and PGID must be numeric" >&2; exit 1 ;;
esac

case "$UMASK" in
  *[!0-7]*|'') echo "UMASK must be an octal value" >&2; exit 1 ;;
esac

umask "$UMASK"

if [ "$(id -u)" = "0" ]; then
  mkdir -p /state
  chown -R "$PUID:$PGID" /state
  exec gosu "$PUID:$PGID" "$@"
fi

if [ "$(id -u)" != "$PUID" ] || [ "$(id -g)" != "$PGID" ]; then
  echo "Warning: the container runtime identity overrides PUID/PGID; running as $(id -u):$(id -g)" >&2
fi

exec "$@"
