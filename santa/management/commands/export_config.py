import json
import sys

from django.core.management.base import BaseCommand

from santa.config_io import export_config


class Command(BaseCommand):
    help = "Export the groups, release sources and manual rules as JSON (without the sync tokens)"

    def add_arguments(self, parser):
        parser.add_argument("-o", "--output", default="-", help="File to write, - for stdout (default)")

    def handle(self, *args, **options):
        content = json.dumps(export_config(), indent=2, ensure_ascii=False) + "\n"
        if options["output"] == "-":
            sys.stdout.write(content)
        else:
            with open(options["output"], "w", encoding="utf-8") as f:
                f.write(content)
            self.stderr.write(f"Configuration written to {options['output']}")
