#!/bin/sh
# Entrypoint of the container image.
#
#   WAIT_FOR_URL       before the web server, wait until this URL answers (e.g. a proxy sidecar of the web pod)
#   WAIT_FOR_URL_JOBS  "1" to wait before every other command too (jobs that have the sidecar as well)
#   WAIT_FOR_TIMEOUT   give up after this many seconds and exit 1, so the platform retries (default 0 = no limit)
#   RUN_MIGRATIONS     "0" to skip the database migrations at the start of the web server (run them as a job instead)
#
# Any other command runs as is, e.g. `python manage.py sync_release_sources` for the scheduled jobs.
set -e

case "$1" in
    gunicorn|*/gunicorn) web_server=1 ;;
    *) web_server=0 ;;
esac

timeout="${WAIT_FOR_TIMEOUT:-0}"
case "$timeout" in
    ''|*[!0-9]*)
        echo "WAIT_FOR_TIMEOUT must be a number of seconds, not '$timeout'" >&2
        exit 1
        ;;
esac

# jobs often run without the sidecar of the web pod: waiting for it there would never end
if [ -n "$WAIT_FOR_URL" ] && { [ "$web_server" = 1 ] || [ "${WAIT_FOR_URL_JOBS:-0}" = 1 ]; }; then
    echo "Waiting for $WAIT_FOR_URL"
    start=$(date +%s)
    until python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5)" "$WAIT_FOR_URL" \
            2>/dev/null; do
        if [ "$timeout" -gt 0 ] && [ $(($(date +%s) - start)) -ge "$timeout" ]; then
            echo "Gave up waiting for $WAIT_FOR_URL after $timeout s" >&2
            exit 1
        fi
        sleep 2
    done
fi

if [ "$web_server" = 1 ] && [ "${RUN_MIGRATIONS:-1}" != "0" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
