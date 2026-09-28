from django.core.cache import cache
from django.core.management.base import BaseCommand

from santa.models import ReleaseSource
from santa.releases import ReleaseError, enable_due_releases, http_session, sync_release_source

LOCK_KEY = "santa:sync_release_sources"


class Command(BaseCommand):
    help = "Check the release sources for new versions and allowlist their binaries"

    def add_arguments(self, parser):
        parser.add_argument("--source", type=int, action="append", help="Only this release source ID")

    def handle(self, *args, **options):
        # the cronjob may overlap with a manual run, the lock lives in the shared Redis cache
        if not cache.add(LOCK_KEY, "1", timeout=3600):
            self.stderr.write("Another sync is running")
            return
        try:
            sources = ReleaseSource.objects.filter(is_enabled=True)
            if options["source"]:
                sources = sources.filter(pk__in=options["source"])
            session = http_session()
            failures = 0
            for source in sources:
                try:
                    release_versions = sync_release_source(source, session)
                except ReleaseError as e:
                    failures += 1
                    self.stderr.write(f"{source}: {e}")
                    release_versions = e.new_versions
                    if not release_versions:
                        continue
                for release_version in release_versions:
                    self.stdout.write(f"{release_version}: new version ({release_version.binary_count} binaries)")
                if not release_versions:
                    self.stdout.write(f"{source}: up to date")
            # also when a source could not be checked, the waiting releases are already downloaded
            for release_version in enable_due_releases():
                self.stdout.write(f"{release_version.source}: version {release_version.version} "
                                  "enabled after the delay")
        finally:
            cache.delete(LOCK_KEY)
        if failures:
            raise SystemExit(1)
