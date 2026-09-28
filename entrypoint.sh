#!/bin/sh
# Entrypoint of the container image.
#
#   WAIT_FOR_URL    wait until this URL answers (e.g. a proxy sidecar) before starting
#   RUN_MIGRATIONS  "0" to skip the database migrations at the start of the web server (run them as a job instead)
#
# Any other command runs as is, e.g. `python manage.py sync_release_sources` for the scheduled jobs.
set -e

if [ -n "$WAIT_FOR_URL" ]; then
    echo "Waiting for $WAIT_FOR_URL"
    until python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5)" "$WAIT_FOR_URL" \
            2>/dev/null; do
        sleep 2
    done
fi

case "$1" in
    gunicorn|*/gunicorn)
        if [ "${RUN_MIGRATIONS:-1}" != "0" ]; then
            python manage.py migrate --noinput
        fi
        ;;
esac

exec "$@"
