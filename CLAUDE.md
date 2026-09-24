# CLAUDE.md

## Temporary files and test artifacts

- **Never use `/tmp` for temporary files.** On this machine `/tmp` is a RAM-backed tmpfs with a
  hard limit of about 1M inodes, shared by every process. A few runs of the e2e suite (about
  100k files each) plus scratch copies exhausted it once. Use `/var/tmp` instead, for example
  `/var/tmp/dstui-<purpose>/`. This covers scratch files, git worktrees, clones, build output and
  anything else that is not meant to stay.
- **Remove all test artifacts after tests have finished.** That includes pytest temp directories,
  runtime homes (`DSH_HOME` directories), scratch copies of the project and build or installer
  output. Nothing may pile up between runs.
- The test suite enforces both rules itself (`tests/tmp_hygiene.py`): each run gets a private
  `/var/tmp/dstui-pytest-<pid>-<random>/` for `tmp_path` and for `TMPDIR` (inherited by the runtime
  and all other child processes), deletes it when the session ends, pass or fail, and sweeps the
  directories of killed runs at the next start. So no `--basetemp` and no `rm` are needed:

  ```sh
  uv run pytest
  ```

## Packaging

`make package` builds `dist/dstui-install.sh` (`tools/package/build-binary.sh`), a **makeself**
self-extracting installer for **linux-x86_64 (glibc)**, about 22 MB. It follows the method of
devitops-com/aiagent:
- a bundled uv-managed CPython, **sourceless** (`.pyc` only)
- a **zstd -19** payload, unpacked by a bundled static zstd
- SHA256 integrity checking and an **`-I`** (isolated) launcher; only `dstui` goes on PATH

Run `make lock` and `make dev-install` first.

- **The installer is the only distribution.** dstui is not published to PyPI; the
  `Private :: Do Not Upload` classifier makes PyPI reject an accidental upload (a test pins it).
- **CPython is pinned exactly** in `.python-version` (`3.14.7`, the single source of truth for the
  dev venv, CI, the locks' `--python-version` and the bundle). `requires-python` and the classifier
  keep the `3.14` floor; a test checks that the two agree.
- **No libpython in the bundle.** The PBS `bin/python3.14` has libpython linked in statically;
  the shared `libpython3.14.so*` and `libpython3.so` (32 MB, for embedding only) and
  `lib/pkgconfig` are dropped. `tools/package/check-python.sh` (tested in
  `tests/test_bundle_python.py`) fails the build unless the staged interpreter reports exactly
  `.python-version`, no `libpython*` is left, and no ELF in the tree NEEDs one (`readelf -d`).
- **Never ship the DeepSeek runtime or Node.** DeepSeek Harness (`dsh`, npm `@deepseek-ai/dsh`,
  Node >= 22.19) is installed separately.
  - `deepseek-harness-runtime-bin` is a **test-only** dev-extra dependency.
  - `make lock` leaves it out of `requirements.txt` (`--no-emit-package`) and keeps it in
    `requirements-dev.txt`.
  - The build installs `requirements.txt` and the wheel with `--no-deps --require-hashes`. That is
    why `pydantic` is listed explicitly.
  - `tools/package/check-no-runtime.sh` fails the build on any runtime, `dsh`, Node or `node_modules`
    file.
- **Locks.** All three are fully hashed:
  - `requirements.txt`: what the installer bundles.
  - `requirements-dev.txt`: what dev-install and CI install.
  - `requirements-build.txt`: the build backend for the shipped wheel.

  There is no `uv.lock`, and `[tool.uv] managed = false`.
- **dsh resolution** (`config.resolve_dsh_bin`, called in `main()`) goes in this order:
  1. `--dsh-bin`
  2. `dsh` on PATH, absolute entries only
  3. the SDK's embedded runtime, if it can be imported (dev and test installs only)

  With none of these, dstui exits 1. The tests always use the embedded runtime.
- **Installer invariants** are tested in `tests/test_installer.py`:
  - a root install is root-owned and readable by everyone
  - the payload contains no build-host paths
  - a relative, whitespace or too-long prefix is resolved or refused before anything is unpacked
  - a noexec `$TMPDIR` works
  - the staged interpreter must run before an existing install is replaced
  - the extraction dir defaults to `/var/tmp`, never `/tmp`
  - `install.sh` with `DSTUI_VERIFY=1` runs the installer only after `gh attestation verify`
    succeeds (fails closed without `gh`)
- **Smoke test** (inside `make package`) installs into a temp prefix and checks:
  - module versions against the lock, and that no runtime or Node file is present
  - `--help` and `--version`, also with hostile `PYTHONPATH`/`PYTHONHOME`
  - that dstui exits 1 when no `dsh` is found
  - one real agent turn by the bundled interpreter, with `--dsh-bin` pointing at the dev venv's
    embedded runtime as a stand-in, against `tests/fake_deepseek.py`

## Release

Releases are built, attested and published by GitHub Actions, not locally. **This departs from
devitops-com/aiagent** (whose `release.sh` builds and publishes on the maintainer's machine):
GitHub artifact attestations can only be made inside GitHub Actions. Porting this flow, and all
the other installer fixes made here, to aiagent is deferred until dstui is done.

- **`make release`** (`tools/release/release.sh`) only tags. It takes the version from
  `pyproject.toml`.
  - **Guards:** on `main`, a clean tree (untracked files and assume-unchanged / skip-worktree
    entries included), in sync with origin, and the tag and release must not exist yet.
  - **Steps:** promote the CHANGELOG `[Unreleased]` section (an empty one is refused, via
    `tools/release/release-notes.sh`), commit `chore: release vX.Y.Z`, create an annotated tag,
    push both atomically. No build and no `gh release create`.
  - Then it waits up to `DSTUI_RELEASE_WATCH_WAIT` seconds (default 60) for the tag's run of
    `release.yml`, follows it with `gh run watch` and prints the release URL, or the recovery
    for a failed run. If no run shows up it says where to follow it and still succeeds.
  - **Non-interactive release:** set `DSTUI_RELEASE_ASSUME_YES=1`.
- **`release.yml`** runs on pull requests, pushes to `main` and `v*` tags.
  - Job `package` (read-only token, on every run): checks on a tag that it matches
    `pyproject.toml`, sets up uv (pinned), installs makeself, runs `make dev-install` and
    `make package` (full smoke test) and uploads `dist/dstui-install.sh`. So a PR proves the
    release build before any tag exists.
  - Job `publish` (tag pushes only; the only job with `contents: write`, `id-token: write`,
    `attestations: write`): extracts the version's notes with `release-notes.sh`, attests
    `dist/dstui-install.sh` and `install.sh` (`actions/attest-build-provenance`) and runs
    `gh release create vX.Y.Z --verify-tag --title "dstui vX.Y.Z" --notes-file …` with both
    files.
  - A concurrency group per ref builds a tag once.
- **`install.sh`** is the `curl … | sh` bootstrap. It is HTTPS-only, honours `DSTUI_PREFIX` and
  `DSTUI_VERSION`, and stages under `$TMPDIR` (default `/var/tmp`). `DSTUI_VERIFY=1` (opt-in)
  runs `gh attestation verify <download> --repo ksparavec/dstui` first and fails closed: no `gh`,
  a failed check or any other `DSTUI_VERIFY` value than `0`/`1` means exit 1, nothing run, the
  download removed.
- **CI:**
  - Every workflow pins its actions by SHA and checks out without persisting the token (a test
    checks both).
  - `ci.yml` runs ruff, mypy --strict, bandit, and the tests with coverage, with bubblewrap/userns
    enabled for the `sdk` profile. It also builds the wheel and sdist, and checks that the wheel
    runs without the embedded runtime.
  - `audit.yml` runs pip-audit on all three locks, daily and on every lock change.
  - `actions/setup-python` takes `.python-version`; 3.14.7 is in its versions manifest.
