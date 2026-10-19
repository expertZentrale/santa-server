"""Compare the hashes the server predicts for Homebrew bottles (santa/bottles.py) with the ones of a Mac.

In the devcontainer, with network access, against the JSON of homebrew_installed_hashes.py from the test Mac:

    python scripts/check_homebrew_hashes.py homebrew-hashes-27.0-20261019.json [--details report.json]

Without a JSON, --probe only shows what the server makes of the current bottles of some formulae (no Mac needed):

    python scripts/check_homebrew_hashes.py --probe pkgconf git --tag arm64_golden_gate
"""
import argparse
import hashlib
import json
import os
import sys
import tarfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "santa_server.settings_dev")

import django  # noqa: E402

django.setup()

from santa import releases  # noqa: E402

GHCR_CORE = "https://ghcr.io/v2/homebrew/core"


def bottle_asset(formula, version, tag, sha256, root_url=GHCR_CORE):
    name = formula.rsplit("/", 1)[-1]
    if "ghcr.io/v2/" not in (root_url or GHCR_CORE):
        return None
    return releases.Asset(name=f"{name}-{version}.{tag}.bottle.tar.gz",
                          url=f"{root_url or GHCR_CORE}/{releases._ghcr_repository(name)}/blobs/sha256:{sha256}",
                          sha256=sha256, headers={"Authorization": f"Bearer {releases.HOMEBREW_BOTTLE_TOKEN}"},
                          bottle_tag=tag)


def predicted(session, asset):
    """{path: (sha256, cdhashes, warning)} and {path: sha256} of the bottle as it is"""
    with releases.download(session, asset) as fileobj:
        found = {path: (info.sha256, info.cdhashes, info.warning)
                 for path, info in releases.find_binaries(fileobj, asset.name, "", asset.bottle_tag)}
        fileobj.seek(0)
        with tarfile.open(fileobj=fileobj, mode="r:*") as archive:
            plain = {member.name: hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                     for member in archive if member.isfile() and member.name in found}
    return found, plain


def check(session, data):
    results = []
    for keg in data["kegs"]:
        if not keg["executables"]:
            continue
        base = {"formula": keg["formula"], "version": keg["version"], "tag": keg["bottle_tag"]}
        asset = bottle_asset(keg["formula"], keg["version"], keg["bottle_tag"], keg["bottle_sha256"],
                             keg.get("root_url"))
        if not keg["bottle_sha256"] or not keg["bottle_tag"] or asset is None:
            results += [{**base, "path": e["path"], "result": "no bottle known"} for e in keg["executables"]]
            continue
        try:
            found, plain = predicted(session, asset)
        except Exception as e:
            results += [{**base, "path": e2["path"], "result": f"download failed: {e}"} for e2 in keg["executables"]]
            continue
        for executable in keg["executables"]:
            path = executable["path"]
            item = {**base, "path": path, "installed": executable["sha256"]}
            if path not in found:
                item["result"] = "not in the bottle"
            else:
                sha256, cdhashes, warning = found[path]
                item.update(predicted=sha256, bottle=plain.get(path))
                if warning:
                    item.update(result="unpredictable", reason=warning.split("(", 1)[-1].split(")", 1)[0])
                elif sha256 == executable["sha256"]:
                    item["result"] = "match" if sha256 != plain.get(path) else "match (unchanged)"
                elif sha256 == plain.get(path):
                    item["result"] = "mismatch: Homebrew changed what the server left"
                else:
                    item["result"] = "mismatch: changed differently"
            results.append(item)
    return results


def probe(session, formulae, tag):
    results = []
    for formula in formulae:
        response = session.get(f"https://formulae.brew.sh/api/formula/{formula}.json",
                               timeout=30)
        if response.status_code != 200:
            results.append({"formula": formula, "path": "", "result": f"formula: HTTP {response.status_code}"})
            continue
        info = response.json()
        bottle = info.get("bottle", {}).get("stable", {})
        files = bottle.get("files", {})
        if tag not in files:
            results.append({"formula": formula, "path": "", "result": f"no bottle for {tag}"})
            continue
        version = info["versions"]["stable"] + (f"_{info['revision']}" if info.get("revision") else "")
        asset = bottle_asset(formula, version, tag, files[tag]["sha256"], bottle.get("root_url"))
        try:
            found, plain = predicted(session, asset)
        except Exception as e:
            results.append({"formula": formula, "path": "", "result": f"failed: {type(e).__name__}: {e}"})
            continue
        for path, (sha256, _cdhashes, warning) in found.items():
            result = ("unpredictable" if warning else "relocated" if sha256 != plain.get(path) else "unchanged")
            results.append({"formula": formula, "version": version, "tag": tag, "cellar": files[tag]["cellar"],
                            "path": path, "result": result,
                            "reason": warning.split("(", 1)[-1].split(")", 1)[0] if warning else ""})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hashes", nargs="?", help="the JSON of homebrew_installed_hashes.py")
    parser.add_argument("--probe", nargs="+", help="formulae to try without a Mac")
    parser.add_argument("--tag", default="arm64_golden_gate", help="the bottle tag for --probe")
    parser.add_argument("--details", help="write every result to this JSON file")
    args = parser.parse_args()
    session = releases.http_session()
    if args.probe:
        results = probe(session, args.probe, args.tag)
    elif args.hashes:
        results = check(session, json.loads(Path(args.hashes).read_text()))
    else:
        parser.error("a JSON file or --probe")
    for item in results:
        if not item["result"].startswith(("match", "relocated", "unchanged")):
            print(f"{item['result']:<48} {item['formula']} {item.get('version', '')} {item['path']} "
                  f"{item.get('reason', '')}")
    counts = Counter(item["result"] for item in results)
    print()
    for result, count in counts.most_common():
        print(f"{count:6}  {result}")
    reasons = Counter(item["reason"] for item in results if item.get("reason"))
    for reason, count in reasons.most_common():
        print(f"{count:6}    {reason}")
    if args.details:
        Path(args.details).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
