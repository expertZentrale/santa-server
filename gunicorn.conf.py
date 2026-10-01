"""gunicorn settings of the image (gunicorn reads ./gunicorn.conf.py from /code by itself).

Workers, threads, worker class, timeout and max requests come from the GUNICORN_* environment variables (the
configuration table of the README). Bind, logging, the heartbeat directory, the graceful timeout and the control
socket are fixed here. Flags on the command line still win over this file.
"""
import os
import sys


def env_int(name, default, minimum=1):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        number = None
    if number is None or number < minimum:
        sys.exit(f"{name} must be a whole number >= {minimum}, not {value!r}")
    return number


bind = "0.0.0.0:8000"
# gthread: a long request (a package rule check, an upload) holds one thread, the worker keeps answering with the
# others and its heartbeat goes on. gevent / eventlet don't fit: the SQL Server driver would block their event loop.
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "").strip() or "gthread"
workers = env_int("GUNICORN_WORKERS", 2)
# gunicorn uses gthread anyway when threads > 1, so the sync worker gets one thread unless more are asked for
threads = env_int("GUNICORN_THREADS", 1 if worker_class == "sync" else 4)
timeout = env_int("GUNICORN_TIMEOUT", 120)
# a new worker after about this many requests, so a slowly growing worker never reaches the memory limit (0 = never)
max_requests = env_int("GUNICORN_MAX_REQUESTS", 1000, minimum=0)
max_requests_jitter = max_requests // 10
# below the 30 s that Kubernetes waits after SIGTERM
graceful_timeout = 25
# the heartbeat of the workers in memory, not on the overlay disk of the container, which can stall
worker_tmp_dir = "/dev/shm"
# the control socket (gunicorn 25+) would go into the home directory, the image user has none; the container is
# managed with signals by the platform
control_socket_disable = True

accesslog = "-"
# the PID of the worker as <pid> (to find a restarted one) and the duration in ms
access_log_format = '%(p)s %(h)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(M)sms "%(a)s"'
