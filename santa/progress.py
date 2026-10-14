"""The progress of a long request (checking a package rule, unpacking an upload, saving many rules).

The console sends an ID in the field progress_id of the form; the request writes how far it is to the cache under
that ID, and the page asks for it (console:progress) while it waits. Without an ID (the scheduled job, the Django
admin) the progress is only counted.

While the request runs, a heartbeat refreshes the entry every few seconds. When the server crashes, a worker is
killed or restarted, the heartbeat stops: the page sees that the action died, also when no error reaches it (a
proxy or a forwarded port keeps the connection open).
"""
import re
import threading
import time

from django.core.cache import cache

PROGRESS_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TIMEOUT = 600
HEARTBEAT = 10


def _key(user, progress_id):
    # per user: nobody reads the progress (the names of packages and files) of someone else's request
    return f"santa:progress:{user.pk}:{progress_id}"


class Progress:
    """Step `current` of `total` (0: not known), working on `label`. A context manager: the heartbeat runs inside."""

    def __init__(self, key=None):
        self.key = key
        self.total = 0
        self.current = 0
        self.label = ""
        self.done = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if self.key:
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
        started = time.monotonic()
        while not self._stop.wait(HEARTBEAT) and time.monotonic() - started < TIMEOUT:
            self._save()

    def add_total(self, count):
        self.total += count
        self._save()

    def start(self, label):
        self.current += 1
        self.note(label)

    def note(self, label):
        """What it works on, without a new step"""
        self.label = str(label)[:200]
        self._save()

    def _save(self):
        if not self.key:
            return
        with self._lock:
            cache.set(self.key, {"total": self.total, "current": self.current, "label": self.label,
                                 "beat": time.time(), "done": self.done}, TIMEOUT)


def progress_for(request):
    # a field of the form (data-progress, core.js): also when the browser sends the form without htmx
    progress_id = request.POST.get("progress_id", "") if request.method == "POST" else ""
    if request.user.is_authenticated and PROGRESS_ID.match(progress_id):
        return Progress(_key(request.user, progress_id))
    return Progress()


def read_progress(user, progress_id):
    """The entry with beat_age: the seconds since the last sign of life of the request"""
    if not PROGRESS_ID.match(progress_id):
        return None
    entry = cache.get(_key(user, progress_id))
    if entry is None:
        return None
    beat = entry.pop("beat", None)
    entry["beat_age"] = round(time.time() - beat, 1) if beat else None
    return entry
