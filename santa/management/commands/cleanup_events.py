from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from santa.models import Event


class Command(BaseCommand):
    help = "Delete old events"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--keep-unresolved", action="store_true", help="Do not delete unresolved events")

    def handle(self, *args, **options):
        events = Event.objects.filter(execution_time__lt=timezone.now() - timedelta(days=options["days"]))
        if options["keep_unresolved"]:
            events = events.filter(resolved_at__isnull=False)
        # delete in chunks, to keep the SQL Server transactions small
        total = 0
        while True:
            pks = list(events.values_list("pk", flat=True)[:1000])
            if not pks:
                break
            total += Event.objects.filter(pk__in=pks).delete()[0]
        self.stdout.write(f"{total} event(s) deleted")
