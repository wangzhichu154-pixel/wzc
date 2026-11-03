#!/bin/sh
set -eu
umask 077
case "${REDIS_PASSWORD:-}" in
    ''|*[!A-Za-z0-9_-]*) echo 'REDIS_PASSWORD must be a nonempty URL-safe secret' >&2; exit 1 ;;
esac
cat > /tmp/redis.conf <<EOF
bind 0.0.0.0
port 6379
protected-mode yes
requirepass ${REDIS_PASSWORD}
dir /data
appendonly yes
appendfsync everysec
maxmemory-policy noeviction
save 900 1
EOF
exec redis-server /tmp/redis.conf
