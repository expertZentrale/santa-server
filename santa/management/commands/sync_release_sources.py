import logging
import signal
import threading

from django.core.cache import cache
from django.core.management.base import BaseCommand

from santa.models import ReleaseSource
from santa.progress import SHUTDOWN, SHUTTING_DOWN, AlreadyRunning, run_source_check
from santa.releases import (
    ReleaseError,
    enable_due_releases,
    http_session,
    rerecord_homebrew_versions,
    sync_release_source,
)

logger = logging.getLogger(__name__)

LOCK_KEY = "santa:sync_release_sources"
# held while the job runs, refreshed by a heartbeat: a job that was killed blocks the next run only this long
LOCK_TIMEOUT = 120
LOCK_HEARTBEAT = 30


def _hold(stop):
    while not stop.wait(LOCK_HEARTBEAT):
        cache.touch(LOCK_KEY, LOCK_TIMEOUT)


class Command(BaseCommand):
    help = "Check the release sources for new versions and allowlist their binaries"

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, action="append", help="Only this release source ID")
        parser.add_argument("--rerecord-homebrew", action="store_true",
                            help="Download the recorded versions of the Homebrew formulae again and replace their "
                                 "rules with the hashes of the installed executables (once after the update)")

    def handle(self, *args, **options):
        # the cronjob may overlap with a manual run, the lock lives in the shared Redis cache
        if not cache.add(LOCK_KEY, "1", timeout=LOCK_TIMEOUT):
            self.stderr.write("Another sync is running")
            return
        stop = threading.Event()
        threading.Thread(target=_hold, args=(stop,), daemon=True, name="santa-sync-lock").start()
        # stopped (the pod of the job ends): the check stops after its current package and is recorded
        # (signals only reach the main thread: not when called from a request)
        main = threading.current_thread() is threading.main_thread()
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: SHUTTING_DOWN.set()) if main else None
        try:
            sources = ReleaseSource.objects.filter(is_enabled=True)
            if options["source"]:
                sources = sources.filter(pk__in=options["source"])
            session = http_session()
            failures = 0
            for source in sources:
                if SHUTTING_DOWN.is_set():
                    self.stderr.write("Stopped: the process is shutting down")
                    break
                try:
                    # seen in the console like a check started there, and stopped there too
                    with run_source_check(source) as progress:
                        # under the lock of the source: not while a check of the console works on its versions
                        if options["rerecord_homebrew"] and source.kind == ReleaseSource.Kind.HOMEBREW_FORMULA:
                            renewed, errors = rerecord_homebrew_versions(session, source, progress)
                            self.stdout.write(f"{source}: {renewed} versions recorded again")
                            for error in errors:
                                self.stderr.write(f"{source}: {error}")
                        release_versions = sync_release_source(source, session, progress=progress)
                except AlreadyRunning:
                    self.stdout.write(f"{source}: checked in the console right now, skipped")
                    continue
                except ReleaseError as e:
                    failures += 1
                    self.stderr.write(f"{source}: {e}")
                    release_versions = e.new_versions
                    if not release_versions:
                        continue
                except Exception as e:
                    # one source never stops the others, nor enabling the due releases below
                    failures += 1
                    logger.exception("Release source %s: check failed", source)
                    self.stderr.write(f"{source}: {type(e).__name__}: {e}")
                    continue
                for release_version in release_versions:
                    self.stdout.write(f"{release_version}: new version ({release_version.binary_count} binaries)")
                if progress.cancelled_by == SHUTDOWN:
                    self.stdout.write(f"{source}: check stopped, the process is shutting down")
                elif progress.cancelled:
                    self.stdout.write(f"{source}: check cancelled by {progress.cancelled_by}")
                elif not release_versions:
                    self.stdout.write(f"{source}: up to date")
            # also when a source could not be checked, the waiting releases are already downloaded
            for release_version in enable_due_releases():
                self.stdout.write(f"{release_version.source}: version {release_version.version} "
                                  "enabled after the delay")
        finally:
            stop.set()
            if main:
                signal.signal(signal.SIGTERM, previous)
            cache.delete(LOCK_KEY)
        if failures:
            raise SystemExit(1)
