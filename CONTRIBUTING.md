# Contributing

Thanks for helping! Changes land through **pull requests** against `main`; the CI must be green.

## Development setup

Open the repository in VS Code → **Reopen in Container** (or any devcontainer tool). It starts SQL Server 2022 and
Redis, creates the database and runs the migrations. See the *Development* section of the [README](README.md).

## Before you open a pull request

Run in the devcontainer:

```bash
ruff check .
python manage.py makemigrations --check --dry-run
python manage.py test
```

- New behaviour comes with tests in `santa/tests/`. Tests never reach the network (mock the HTTP session).
- Every model change comes with a migration. Everything must work on SQL Server.
- User-facing texts are English and translatable. After changing texts, update the German translation:
  `python manage.py makemessages -l de`, translate the new entries in `santa/locale/de/LC_MESSAGES/django.po`,
  `python manage.py compilemessages`, and commit the `.po` and the `.mo`.
- New settings are environment variables in `santa_server/settings.py` and documented in the README.
- Keep it a plain Django application: no frontend build, no new framework without discussing it first in an issue.

[AGENTS.md](AGENTS.md) has the detailed rules of the project (they apply to people and AI agents alike).

## Releases

Maintainers tag releases as `vMAJOR.MINOR.PATCH`; the image workflow publishes `ghcr.io/expertzentrale/santa-server` with
the version tags and `latest`.

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
