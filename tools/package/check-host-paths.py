"""Fail if the staged payload discloses the build host.

    python -I check-host-paths.py REQUIREMENTS STAGE_DIR PATTERN [PATTERN ...]

build-binary.sh passes the build's own paths as fixed-string PATTERNs: the uv-managed CPython
it staged, the project checkout and $HOME/. Every regular file under STAGE_DIR is searched
for each of them, and so is every symlink's target (the tar stores it).

A file byte-identical to what a wheel pinned in REQUIREMENTS shipped is exempt, because it is
upstream content from a hash-pinned wheel and cannot disclose this build host. pydantic_core's
CycloneDX SBOM, for example, names pydantic's own CI checkout (/home/runner/work/pydantic/...),
and $HOME/ is /home/runner/ on a GitHub runner. Such a file is listed in the RECORD of a
*.dist-info whose distribution REQUIREMENTS pins, with a sha256 that matches it. It is still
reported on stdout as upstream content, never as a leak. No other RECORD vouches for anything:
not the dstui wheel's, which is built from the checkout on this host, and not one that is not
the UTF-8 CSV pip writes.

These are not exempt, even when their hash matches:
  - pip's own install-time files, which pip rehashes into RECORD: INSTALLER, REQUESTED and
    direct_url.json (it names the wheel file on the build host)
  - anything outside the RECORD's own directory (a ../ entry), such as the console-script
    launcher pip generates with the staging interpreter in its shebang

Entries without a sha256, files that are gone (the .py sources the build deletes) and files
that differ from their hash exempt nothing. That keeps everything the build compiles or
rewrites (.pyc, _sysconfigdata, the launcher) in the scan.

Exit status: 0 clean, 1 with each offending file and the patterns it contains on stderr (or
with the reason it could not search the whole tree), 2 on bad usage.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import os
import re
import sys
from collections.abc import Iterator

USAGE = "usage: check-host-paths.py REQUIREMENTS STAGE_DIR PATTERN [PATTERN ...]"
INSTALLER_WRITTEN = frozenset({"INSTALLER", "REQUESTED", "direct_url.json", "RECORD"})
PINNED = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", re.MULTILINE)


def normalized(name: str) -> str:
    """PEP 503: case-insensitive, and every run of -, _ and . is one -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_names(requirements: str) -> frozenset[str]:
    """The distributions requirements.txt pins (NAME==VERSION), which pip installs hash-checked."""
    with open(requirements, encoding="utf-8") as f:
        return frozenset(normalized(match[1]) for match in PINNED.finditer(f.read()))


def is_dist_info(path: str) -> bool:
    return os.path.basename(path).endswith(".dist-info")


def dist_name(dist_info: str) -> str:
    """NAME of NAME-VERSION.dist-info, normalized."""
    return normalized(os.path.basename(dist_info).removesuffix(".dist-info").rpartition("-")[0])


def walk(top: str) -> Iterator[tuple[str, list[str], list[str]]]:
    """os.walk, but a directory it cannot list fails the gate instead of going unsearched."""

    def fail(error: OSError) -> None:
        raise error

    return os.walk(top, onerror=fail)


def record_rows(dist_info: str) -> list[list[str]]:
    """``dist_info``/RECORD's rows; none when it is not the UTF-8 CSV pip writes."""
    with open(os.path.join(dist_info, "RECORD"), encoding="utf-8", newline="") as f:
        try:
            return list(csv.reader(f))
        except UnicodeDecodeError, csv.Error:
            return []


def record_digests(site_dir: str, dist_info: str) -> Iterator[tuple[str, str]]:
    """(absolute path, sha256 digest) for the rows of ``dist_info``/RECORD that can exempt a
    file: hashed with sha256, inside ``site_dir``, and not pip's install-time metadata of any
    distribution."""
    for row in record_rows(dist_info):
        if len(row) < 2 or not row[1].startswith("sha256="):
            continue
        path = os.path.normpath(os.path.join(site_dir, row[0]))
        if os.path.commonpath([site_dir, path]) != site_dir:
            continue
        if os.path.basename(path) in INSTALLER_WRITTEN and is_dist_info(os.path.dirname(path)):
            continue
        yield path, row[1].removeprefix("sha256=").rstrip("=")


def verbatim_digests(stage: str, pinned: frozenset[str]) -> dict[str, set[str]]:
    """Every file path the RECORD of a ``pinned`` distribution under ``stage`` vouches for,
    with the digests it may have."""
    digests: dict[str, set[str]] = {}
    for dirpath, dirnames, _filenames in walk(stage):
        for name in dirnames:
            dist_info = os.path.join(dirpath, name)
            if (
                is_dist_info(name)
                and dist_name(name) in pinned
                and os.path.isfile(os.path.join(dist_info, "RECORD"))
            ):
                for path, digest in record_digests(dirpath, dist_info):
                    digests.setdefault(path, set()).add(digest)
    return digests


def sha256_digest(data: bytes) -> str:
    """As RECORD writes it: urlsafe base64 without padding."""
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def payload(stage: str) -> Iterator[tuple[str, bytes, bytes | None]]:
    """(label, bytes to search, file content) for every regular file and every symlink under
    ``stage``. A symlink's target is what the tar stores, so that is what gets searched; it has
    no content a RECORD could vouch for."""
    for dirpath, dirnames, filenames in walk(stage):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                target = os.readlink(path)
                yield f"{path} -> {target}", os.fsencode(target), None
            elif name in filenames and os.path.isfile(path):
                with open(path, "rb") as f:
                    data = f.read()
                yield path, data, data


def main(argv: list[str]) -> int:
    if len(argv) < 3 or not all(argv):
        print(USAGE, file=sys.stderr)
        return 2
    if not os.path.isfile(argv[0]):
        print(f"check-host-paths.py: not a file: {argv[0]}", file=sys.stderr)
        return 2
    if not os.path.isdir(argv[1]):
        print(f"check-host-paths.py: not a directory: {argv[1]}", file=sys.stderr)
        return 2
    stage, patterns = os.path.abspath(argv[1]), argv[2:]
    verbatim = verbatim_digests(stage, pinned_names(argv[0]))
    leaks: list[str] = []
    upstream: list[str] = []
    exempt = 0
    for label, searched, content in payload(stage):
        is_verbatim = (
            content is not None and label in verbatim and sha256_digest(content) in verbatim[label]
        )
        exempt += is_verbatim
        found = [p for p in patterns if os.fsencode(p) in searched]
        if found:
            (upstream if is_verbatim else leaks).append(f"{label}: {', '.join(found)}")
    for line in sorted(upstream):
        print(f"    upstream, verbatim from its wheel's RECORD: {line}")
    if leaks:
        print("ERROR: build-host paths in the payload:", file=sys.stderr)
        print(*(f"  {line}" for line in sorted(leaks)), sep="\n", file=sys.stderr)
        return 1
    print(f"    ok: no build-host paths (upstream files verified against their RECORD: {exempt})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except OSError as error:
        sys.exit(f"ERROR: cannot search the payload for build-host paths: {error}")
