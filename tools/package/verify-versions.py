"""Confirm every module in a built bundle matches the version pinned in the lock.

Two independent layers, because the two can disagree (a dist-info of one version on
disk while the code of another is what actually gets imported, e.g. from a stale or
poisoned cached wheel):

  1. metadata  — ``importlib.metadata.version(name)`` == pinned  (all pinned pkgs)
  2. code      — ``<top-module>.__version__`` == pinned          (those exposing it)

Run under the BUNDLE's own interpreter, from this on-disk file. Usage:

    python verify-versions.py <requirements.txt> [pkg-intentionally-absent ...]

Exits non-zero with a report if any pinned module is stale (wrong version) or
unexpectedly missing. Packages the build deliberately leaves out are passed as
trailing args so their absence is not treated as a failure. (dstui's own
requirements.txt never pins the SDK's embedded runtime, deepseek-harness-runtime-bin:
`make lock` leaves it out, so it needs no allow-listing here.)
"""

from __future__ import annotations

import importlib
import importlib.metadata as im
import re
import sys
import warnings

warnings.filterwarnings("ignore")  # some packages deprecate `__version__` access

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._!+-]*)")


def _canon(name: str) -> str:
    """PEP 503 canonical distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


# PEP 440's own version grammar (Appendix B), so the comparison needs no third-party
# `packaging`: the bundle does not carry it, and the dev venv must not be put on
# sys.path (it would shadow the very modules being audited).
_PEP440 = re.compile(
    r"""
    v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>alpha|a|beta|b|preview|pre|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>-(?P<post_n1>[0-9]+)|[-_.]?(?:post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?)?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    """,
    re.VERBOSE | re.IGNORECASE,
)
_PRE = {"alpha": "a", "beta": "b", "c": "rc", "pre": "rc", "preview": "rc"}


def _pep440_key(version: str) -> tuple[object, ...] | None:
    """A key equal for exactly the versions PEP 440 considers equal; None if unparsable."""
    m = _PEP440.fullmatch(version.strip())
    if m is None:
        return None
    release = [int(part) for part in m["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()  # 1.0 == 1.0.0
    pre = None
    if m["pre_l"]:
        label = m["pre_l"].lower()
        pre = (_PRE.get(label, label), int(m["pre_n"] or 0))
    post = int(m["post_n1"] or m["post_n2"] or 0) if m["post"] else None
    dev = int(m["dev_n"] or 0) if m["dev"] else None
    local = None
    if m["local"]:
        local = tuple(
            int(seg) if seg.isdigit() else seg for seg in re.split(r"[-_.]", m["local"].lower())
        )
    return (int(m["epoch"] or 0), tuple(release), pre, post, dev, local)


def _ver_eq(a: str, b: str) -> bool:
    """Compare two version strings with PEP 440 normalization (1.0 == 1.0.0,
    0.1.5rc1 == 0.1.5.rc1, 2026.06.17 == 2026.6.17); unparsable ones must match exactly."""
    a, b = a.strip(), b.strip()
    if a == b:
        return True
    key_a, key_b = _pep440_key(a), _pep440_key(b)
    return key_a is not None and key_a == key_b


def _parse_pins(path: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    with open(path, encoding="utf-8") as lock:
        for raw in lock:
            if not raw[:1].strip() or raw.lstrip().startswith(("#", "-")):
                continue  # skip hash / comment / option continuation lines
            m = _PIN.match(raw.strip())
            if m:
                pins[_canon(m.group(1))] = m.group(2)
    return pins


def _top_modules(dist: im.Distribution) -> list[str]:
    top = dist.read_text("top_level.txt")
    if top:
        return [m for m in top.split() if m and not m.startswith("_")]
    name = (dist.metadata["Name"] or "").replace("-", "_")
    return [name] if name else []


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: verify-versions.py <requirements.txt> [absent-pkg ...]", file=sys.stderr)
        return 2
    pins = _parse_pins(sys.argv[1])
    allow_missing = {_canon(a) for a in sys.argv[2:]}
    if not pins:
        print("ERROR: no pins parsed from requirements.txt", file=sys.stderr)
        return 1

    problems: list[str] = []

    # Layer 1: metadata version for every pinned distribution.
    for name, want in sorted(pins.items()):
        try:
            got = im.version(name)
        except im.PackageNotFoundError:
            if name not in allow_missing:
                problems.append(f"MISSING   {name}: not installed (want {want})")
            continue
        if not _ver_eq(got, want):
            problems.append(f"METADATA  {name}: dist-info {got} != requirements {want}")

    # Layer 2: imported code's __version__ for pinned dists that expose it.
    checked_code = 0
    seen: set[str] = set()
    for dist in im.distributions():
        dname = _canon(dist.metadata["Name"] or "")
        if dname not in pins or dname in seen:
            continue
        seen.add(dname)
        for mod in _top_modules(dist):
            try:
                obj = importlib.import_module(mod)
            except Exception:
                continue  # not importable standalone; layer 1 vouches for presence
            rt = getattr(obj, "__version__", None)
            if isinstance(rt, str) and rt.strip():
                checked_code += 1
                if not _ver_eq(rt, pins[dname]):
                    problems.append(
                        f"CODE      {dname}: {mod}.__version__ {rt.strip()} "
                        f"!= requirements {pins[dname]}"
                    )
                break  # one representative top module per distribution is enough

    if problems:
        print(f"FAIL: {len(problems)} module version problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(
        f"OK: {len(pins)} pinned modules match requirements.txt "
        f"(metadata + {checked_code} code __version__ checks)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
