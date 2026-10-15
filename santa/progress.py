"""The progress of a long request (checking a package rule, unpacking an upload, saving many rules).

The console sends an ID in the field progress_id of the form; the request writes how far it is to the cache under
that ID, and the page asks for it (console:progress) while it waits. Without an ID (the scheduled job, the Django
admin) the progress is only counted.

While the request runs, a heartbeat refreshes the entry every few seconds. When the server crashes, a worker is
killed or restarted, the heartbeat stops: the page sees that the action died, also when no error reaches it (a
proxy or a forwarded port keeps the connection open).

The check of a package rule also has an entry of its own (run_source_check()): one check per package rule at a time,
seen by everyone who opens it, and stopped by any of them (cancel_source_check()). The request goes on until the
check reaches the next package.
"""
import re
import threading
import time
from contextlib import contextmanager

from django.core.cache import cache

PROGRESS_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMEOUT = 600
HEARTBEAT = 10
# no heartbeat for this long: the request died (core.js waits as long)
DEAD_AFTER = 35


class Cancelled(Exception):
    """Somebody stopped the request (the cancel flag of its entry), or the server is shutting down"""


# The server stops (SIGTERM: gunicorn.conf.py, the scheduled job): the long requests end after their current step,
# within the graceful timeout, instead of being killed in the middle. cancelled_by is SHUTDOWN then.
SHUTTING_DOWN = threading.Event()
SHUTDOWN = "\x00shutdown"


def _key(user, progress_id):
    # per user: nobody reads the progress (the names of packages and files) of someone else's request
    return f"santa:progress:{user.pk}:{progress_id}"


def _source_key(pk):
    return f"santa:source-check:{pk}"


def _cancel_key(key):
    return f"{key}:cancel"


def user_label(user):
    return (user.get_full_name() or user.get_username()) if user is not None else ""


class Progress:
    """Step `current` of `total` (0: not known), working on `label`. A context manager: the heartbeat runs inside.

    start() and note() raise Cancelled once somebody set the cancel flag of one of its entries.
    """

    def __init__(self, key=None):
        self.keys = [key] if key else []
        self.total = 0
        self.current = 0
        self.label = ""
        self.done = False
        self.cancelled_by = None
        # stored with the steps (run_source_check(): who started it and when)
        self.extra = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    @property
    def cancelled(self):
        return self.cancelled_by is not None

    def __enter__(self):
        if self.keys:
            self._save()
            self._thread = threading.Thread(target=self._beat, daemon=True, name="santa-progress")
            self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        self.done = True
        self._save()
        return False

    def _beat(self):
        # as long as the request runs, also beyond TIMEOUT: the cache entry only outlives it by that long
        while not self._stop.wait(HEARTBEAT):
            self._save()

    def add_total(self, count):
        self.total += count
        self._save()

    def start(self, label):
        self._check_cancelled()
        self.current += 1
        self.note(label)

    def note(self, label):
        """What it works on, without a new step"""
        self._check_cancelled()
        self.label = str(label)[:200]
        self._save()

    def _check_cancelled(self):
        if self.cancelled:
            raise Cancelled
        if SHUTTING_DOWN.is_set():
            self.cancelled_by = SHUTDOWN
            raise Cancelled
        if not self.keys:
            return
        flags = cache.get_many([_cancel_key(key) for key in self.keys])
        if flags:
            self.cancelled_by = next(iter(flags.values()))
            raise Cancelled

    def _save(self):
        with self._lock:
            if not self.keys:
                return
            entry = {**self.extra, "total": self.total, "current": self.current, "label": self.label,
                     "beat": time.time(), "done": self.done}
            cache.set_many({key: entry for key in self.keys}, TIMEOUT)


def progress_for(request):
    # a field of the form (data-progress, core.js): also when the browser sends the form without htmx
    progress_id = request.POST.get("progress_id", "") if request.method == "POST" else ""
    if request.user.is_authenticated and PROGRESS_ID.match(progress_id):
        return Progress(_key(request.user, progress_id))
    return Progress()


def _with_beat_age(entry):
    beat = entry.pop("beat", None)
    entry["beat_age"] = round(time.time() - beat, 1) if beat else None
    return entry


def read_progress(user, progress_id):
    """The entry with beat_age: the seconds since the last sign of life of the request"""
    if not PROGRESS_ID.match(progress_id):
        return None
    entry = cache.get(_key(user, progress_id))
    return _with_beat_age(entry) if entry is not None else None


def cancel_progress(user, progress_id):
    """Stop a request of the user (the preview of a package rule): it stops at its next step"""
    if PROGRESS_ID.match(progress_id) and cache.get(_key(user, progress_id)) is not None:
        cache.set(_cancel_key(_key(user, progress_id)), user_label(user), TIMEOUT)
        return True
    return False


class AlreadyRunning(Exception):
    def __init__(self, check):
        super().__init__("A check of this package rule is running")
        self.check = check


@contextmanager
def run_source_check(source, user=None, progress=None):
    """The check of a package rule, the only one at a time (AlreadyRunning). user None: the scheduled job.

    progress: the Progress of the request (progress_for()), its entry gets the same steps.
    """
    progress = progress or Progress()
    key = _source_key(source.pk)
    progress.extra = {"user": user_label(user), "job": user is None, "started": time.time()}
    claim = {**progress.extra, "total": 0, "current": 0, "label": "", "beat": time.time(), "done": False}
    if not cache.add(key, claim, TIMEOUT):
        running = read_source_check(source.pk)
        # a dead one is gone now (read_source_check() deletes it): one more try
        if running is not None or not cache.add(key, claim, TIMEOUT):
            raise AlreadyRunning(running or read_source_check(source.pk))
    cache.delete(_cancel_key(key))
    progress.keys.append(key)
    try:
        with progress:
            yield progress
    finally:
        with progress._lock:
            progress.keys.remove(key)
        cache.delete_many([key, _cancel_key(key)])


def read_source_check(pk):
    """The running check of a package rule: {user, job, started, total, current, label, beat_age, cancelled_by},
    or None. One without a heartbeat for DEAD_AFTER died with its request and is deleted."""
    key = _source_key(pk)
    entry = cache.get(key)
    if entry is None:
        return None
    entry = _with_beat_age(entry)
    if entry["beat_age"] is not None and entry["beat_age"] > DEAD_AFTER:
        cache.delete_many([key, _cancel_key(key)])
        return None
    entry["cancelled_by"] = cache.get(_cancel_key(key))
    return entry


def read_source_checks(pks):
    """{pk: check} of the package rules being checked (the rows of the list)"""
    pks = list(pks)
    if not pks:
        return {}
    running = cache.get_many([_source_key(pk) for pk in pks])
    return {pk: check for pk in pks if _source_key(pk) in running and (check := read_source_check(pk))}


def cancel_source_check(pk, user):
    """Stop the check of a package rule after its current package; False when none runs"""
    key = _source_key(pk)
    if read_source_check(pk) is None:
        return False
    cache.set(_cancel_key(key), user_label(user) or "?", TIMEOUT)
    return True
