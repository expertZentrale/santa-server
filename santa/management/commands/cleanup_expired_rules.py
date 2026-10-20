import logging

from django.contrib.admin.models import DELETION, LogEntry
from django.core.management.base import BaseCommand
from django.utils import timezone

from santa.models import Rule

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete the rules whose end has come (Rule.expires_at). The Macs dropped them already: they are not sent."

    def handle(self, *args, **options):
        expired = Rule.objects.filter(expires_at__lte=timezone.now()).order_by("pk")
        total = 0
        while True:
            # in chunks: SQL Server takes at most 2100 parameters per query, and the transactions stay small
            rules = list(expired[:1000])
            if not rules:
                break
            # in the history of the rule, as the one who created it (an entry needs a user)
            by_user = {}
            for rule in rules:
                if rule.created_by_id:
                    by_user.setdefault(rule.created_by_id, []).append(rule.pk)
                logger.info("Rule %s %s expired at %s, deleted", rule.pk, rule, rule.expires_at)
            for user_id, pks in by_user.items():
                LogEntry.objects.log_actions(user_id, Rule.objects.filter(pk__in=pks), DELETION,
                                             change_message="Expired", single_object=len(pks) == 1)
            total += Rule.objects.filter(pk__in=[rule.pk for rule in rules]).delete()[1].get("santa.Rule", 0)
        self.stdout.write(f"{total} expired rule(s) deleted")
