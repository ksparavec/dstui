"""Fail if the staged payload discloses the build host.

    python -I check-host-paths.py STAGE_DIR PATTERN [PATTERN ...]

build-binary.sh passes the build's own paths as fixed-string PATTERNs: the uv-managed CPython
it staged, the project checkout and $HOME/. Every regular file under STAGE_DIR is searched
for each of them, and so is every symlink's target (the tar stores it).

A file byte-identical to what its wheel shipped is exempt, because it is upstream content
from a hash-pinned wheel and cannot disclose this build host. pydantic_core's CycloneDX SBOM,
for example, names pydantic's own CI checkout (/home/runner/work/pydantic/...), and $HOME/
is /home/runner/ on a GitHub runner. Such a file is listed in a *.dist-info/RECORD with a
sha256 that matches it. It is still reported on stdout as upstream content, never as a leak.

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
import sys
from collections.abc import Iterator

USAGE = "usage: check-host-paths.py STAGE_DIR PATTERN [PATTERN ...]"
INSTALLER_WRITTEN = frozenset({"INSTALLER", "REQUESTED", "direct_url.json", "RECORD"})


def is_dist_info(path: str) -> bool:
    return os.path.basename(path).endswith(".dist-info")


def walk(top: str) -> Iterator[tuple[str, list[str], list[str]]]:
    """os.walk, but a directory it cannot list fails the gate instead of going unsearched."""

    def fail(error: OSError) -> None:
        raise error

    return os.walk(top, onerror=fail)


def record_digests(site_dir: str, dist_info: str) -> Iterator[tuple[str, str]]:
    """(absolute path, sha256 digest) for the rows of ``dist_info``/RECORD that can exempt a
    file: hashed with sha256, inside ``site_dir``, and not pip's install-time metadata of any
    distribution."""
    with open(os.path.join(dist_info, "RECORD"), encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2 or not row[1].startswith("sha256="):
                continue
            path = os.path.normpath(os.path.join(site_dir, row[0]))
            if os.path.commonpath([site_dir, path]) != site_dir:
                continue
            if os.path.basename(path) in INSTALLER_WRITTEN and is_dist_info(os.path.dirname(path)):
                continue
            yield path, row[1].removeprefix("sha256=").rstrip("=")


def verbatim_digests(stage: str) -> dict[str, set[str]]:
    """Every file path any RECORD under ``stage`` vouches for, with the digests it may have."""
    digests: dict[str, set[str]] = {}
    for dirpath, dirnames, _filenames in walk(stage):
        for name in dirnames:
            dist_info = os.path.join(dirpath, name)
            if is_dist_info(name) and os.path.isfile(os.path.join(dist_info, "RECORD")):
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
    if len(argv) < 2 or not all(argv[1:]):
        print(USAGE, file=sys.stderr)
        return 2
    if not os.path.isdir(argv[0]):
        print(f"check-host-paths.py: not a directory: {argv[0]}", file=sys.stderr)
        return 2
    stage, patterns = os.path.abspath(argv[0]), argv[1:]
    verbatim = verbatim_digests(stage)
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
