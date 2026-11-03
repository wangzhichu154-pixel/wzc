#!/bin/sh
set -eu
umask 027
if [ "${1:-}" = "gunicorn" ] && [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
    mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
    find "$PROMETHEUS_MULTIPROC_DIR" -maxdepth 1 -type f -name '*.db' -delete
fi
if [ "${COLLECT_STATIC:-0}" = "1" ]; then
    python manage.py collectstatic --noinput
    # Uploaded datasets stay 0600/0700; only public collected assets are shared.
    find "${STATIC_ROOT:-/app/staticfiles}" -type d -exec chmod 0755 {} +
    find "${STATIC_ROOT:-/app/staticfiles}" -type f -exec chmod 0644 {} +
fi
exec "$@"
