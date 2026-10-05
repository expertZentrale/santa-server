import json
import sys

from django.core.management.base import BaseCommand, CommandError

from santa.config_io import ConfigImportError, import_config


class Command(BaseCommand):
    help = "Import a configuration exported with export_config. Everything or nothing is imported."

    def add_arguments(self, parser):
        parser.add_argument("file", help="JSON file, - for stdin")
        parser.add_argument("--dry-run", action="store_true", help="Show the changes without saving them")
        parser.add_argument("--delete-missing", action="store_true",
                            help="Delete the manual rules, release sources and file access rules that are not in "
                                 "the file")

    def handle(self, *args, **options):
        try:
            if options["file"] == "-":
                data = json.load(sys.stdin)
            else:
                with open(options["file"], encoding="utf-8") as f:
                    data = json.load(f)
        except (OSError, ValueError) as e:
            raise CommandError(f"Could not read the file: {e}")
        try:
            report = import_config(data, delete_missing=options["delete_missing"], dry_run=options["dry_run"])
        except ConfigImportError as e:
            raise CommandError("Nothing imported:\n" + "\n".join(e.errors))
        for action, model, obj in report["changes"]:
            self.stdout.write(f"{action:8} {model}: {obj}")
        for warning in report["warnings"]:
            self.stderr.write(f"warning: {warning}")
        summary = f"{len(report['changes'])} change(s)"
        self.stdout.write(f"Dry run, nothing saved: {summary}" if options["dry_run"] else f"Imported: {summary}")
