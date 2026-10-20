#!/usr/bin/env python3
"""Collect the hashes of Homebrew's installed executables on a Mac, for check_homebrew_hashes.py.

Runs on a test Mac with Homebrew (the system /usr/bin/python3, 3.9, is enough: only the standard library). Santa
must be in Monitor mode while it installs. It installs the formulae (latest version, from bottles), then records
every Mach-O executable of every installed keg (their dependencies too) with the bottle it came from:

    /usr/bin/python3 homebrew_installed_hashes.py pkgconf git --top 300 --from-export santa-config.json
    /usr/bin/python3 homebrew_installed_hashes.py --no-install -o hashes.json
    /usr/bin/python3 homebrew_installed_hashes.py --copy pkgconf/3.0.9/bin/pkgconf -o files.tar.gz

The output is JSON, without the binaries: --copy puts chosen installed files (paths inside the Cellar) into a tar.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ANALYTICS_URL = "https://formulae.brew.sh/api/analytics/install-on-request/30d.json"
MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
MH_EXECUTE = 2


def brew(*args, check=True):
    # brew needs the user's environment (HOME, USER, …)
    env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1",
           "HOMEBREW_NO_ENV_HINTS": "1",
           "PATH": f"/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    # Homebrew asks before it installs dependencies ("Do you want to proceed?"): yes, without a terminal
    result = subprocess.run(["brew", *args], capture_output=True, text=True, env=env, input="y\n" * 10)
    if check and result.returncode:
        raise SystemExit(f"brew {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def top_formulae(count):
    with urllib.request.urlopen(ANALYTICS_URL, timeout=60) as response:
        items = json.load(response)["items"]
    # names like "node@22" or "hashicorp/tap/terraform" (taps) are kept as they are
    return [item["formula"] for item in items[:count]]


def export_formulae(path):
    if not Path(path).is_file():
        raise SystemExit(f"{path} not found: the configuration export of the server (console, Administration), "
                         "or leave out --from-export")
    data = json.loads(Path(path).read_text())
    return [line.strip() for source in data.get("release_sources", []) if source.get("kind") == "HOMEBREW_FORMULA"
            for line in (source.get("identifier") or "").splitlines() if line.strip()]


def is_executable(path):
    """A Mach-O executable: a thin one names its file type, a universal binary is checked per slice by its first"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    if len(head) < 16 or head[:4] not in MACHO_MAGICS:
        return False
    if head[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        # Java class files share the magic: their next word is the class version
        if not 0 < int.from_bytes(head[4:8], "big") < 32:
            return False
        offset = int.from_bytes(head[16:20], "big") if head[:4] == b"\xca\xfe\xba\xbe" else \
            int.from_bytes(head[16:24], "big")
        with open(path, "rb") as handle:
            handle.seek(offset)
            head = handle.read(16)
        if head[:4] not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
            return False
    return int.from_bytes(head[12:16], "little") == MH_EXECUTE


def cdhash(path):
    result = subprocess.run(["codesign", "-dvvv", str(path)], capture_output=True, text=True)
    match = re.search(r"^CDHash=([0-9a-f]+)", result.stderr, re.MULTILINE)
    return match[1] if match else ""


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def keg_bottle(info, version):
    """The bottle tag and SHA-256 a keg was poured from (INSTALL_RECEIPT.json and the formula's bottles)"""
    files = info.get("bottle", {}).get("stable", {}).get("files", {})
    installed = next((item for item in info.get("installed", []) if item.get("version") == version), {})
    tag = ""
    receipt = Path(brew("--cellar").stdout.strip()) / info["name"] / version / "INSTALL_RECEIPT.json"
    if receipt.exists():
        data = json.loads(receipt.read_text())
        tag = (data.get("source") or {}).get("bottle_tag") or ""
    if not tag:
        # the receipt doesn't name the tag: the one Homebrew picks on this Mac
        cache = brew("--cache", "--formula", info["full_name"], check=False).stdout.strip()
        found = re.search(r"\.([a-z0-9_]+)\.bottle(?:\.\d+)?\.tar\.gz$", cache)
        tag = found[1] if found else ""
    current = info.get("versions", {}).get("stable", "")
    revision = info.get("revision") or 0
    current_version = f"{current}_{revision}" if revision else current
    bottle = files.get(tag, {}) if version == current_version else {}
    return {"bottle_tag": tag, "bottle_sha256": bottle.get("sha256", ""), "bottle_cellar": bottle.get("cellar", ""),
            "poured_from_bottle": installed.get("poured_from_bottle"),
            "root_url": info.get("bottle", {}).get("stable", {}).get("root_url", "")}


def collect(cellar):
    infos = json.loads(brew("info", "--json=v2", "--installed").stdout)["formulae"]
    kegs = []
    for info in infos:
        for installed in info.get("installed", []):
            version = installed["version"]
            keg = cellar / info["name"] / version
            if not keg.is_dir():
                continue
            executables = []
            for path in sorted(keg.rglob("*")):
                if path.is_symlink() or not path.is_file() or not is_executable(path):
                    continue
                executables.append({"path": str(path.relative_to(cellar)), "sha256": sha256(path),
                                    "cdhash": cdhash(path)})
            kegs.append({"formula": info["full_name"], "version": version, **keg_bottle(info, version),
                         "executables": executables})
            print(f"{info['full_name']} {version}: {len(executables)} executables", file=sys.stderr)
    return kegs


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("formulae", nargs="*")
    parser.add_argument("--top", type=int, default=0, help="also the most installed formulae (30 days)")
    parser.add_argument("--from-export", help="also the Homebrew formulae of a Santa Server configuration export")
    parser.add_argument("--no-install", action="store_true", help="only record what is installed")
    parser.add_argument("--copy", nargs="+", help="put these installed files (paths in the Cellar) in a tar.gz")
    parser.add_argument("-o", "--output", help="the output file")
    # formulae and options in any order
    args = parser.parse_intermixed_args()
    if os.geteuid() == 0:
        raise SystemExit("Run it as the user who owns Homebrew, not as root")
    cellar = Path(brew("--cellar").stdout.strip())

    if args.copy:
        output = args.output or "homebrew-files.tar.gz"
        with tarfile.open(output, "w:gz") as archive:
            for name in args.copy:
                archive.add(cellar / name, arcname=name)
        print(output)
        return

    formulae = list(args.formulae)
    if args.from_export:
        formulae += export_formulae(args.from_export)
    if args.top:
        formulae += top_formulae(args.top)
    failed = {}
    if not args.no_install:
        for index, formula in enumerate(dict.fromkeys(formulae), 1):
            print(f"[{index}/{len(set(formulae))}] brew install {formula}", file=sys.stderr)
            result = brew("install", "--formula", formula, check=False)
            if result.returncode:
                lines = (result.stderr + result.stdout).strip().splitlines() or ["?"]
                failed[formula] = next((line for line in lines if line.startswith("Error:")), lines[-1])
                print(f"  failed: {failed[formula]}", file=sys.stderr)
    macos = platform.mac_ver()[0]
    output = args.output or f"homebrew-hashes-{macos}-{datetime.now(timezone.utc):%Y%m%d}.json"
    data = {"collected_at": datetime.now(timezone.utc).isoformat(), "macos": macos, "machine": platform.machine(),
            "homebrew": brew("--version").stdout.splitlines()[0], "prefix": brew("--prefix").stdout.strip(),
            "failed": failed, "kegs": collect(cellar)}
    Path(output).write_text(json.dumps(data, indent=1))
    print(output)


if __name__ == "__main__":
    main()
