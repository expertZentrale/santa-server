"""The release notes of a version from CHANGELOG.md, for `gh release create`.

    python scripts/release_notes.py 0.12.0 > notes.md
    gh release create v0.12.0 --notes-file notes.md

The section "## [0.12.0] - …" without its heading, up to the next version. Exits with 1 when the version has no
section: rename "## [Unreleased]" to the version (with the date) before the release.
"""
import pathlib
import re
import sys

CHANGELOG = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def release_notes(text, version):
    """The body of the section of a version, None when there is none"""
    version = version.removeprefix("v")
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text,
                      re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    notes = release_notes(CHANGELOG.read_text(encoding="utf-8"), sys.argv[1])
    if notes is None:
        sys.exit(f"No section for {sys.argv[1]} in {CHANGELOG.name}")
    print(notes)
