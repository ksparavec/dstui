# dstui

A small terminal chat UI for a [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
agent, built with [Textual](https://textual.textualize.io/) on the official Python SDK.

You type a prompt. The agent answers, and its reasoning and tool calls appear as collapsible
blocks. The status bar shows what the agent is doing, the model and profile, and the token
totals. dstui drives a DeepSeek Harness runtime process (`dsh`), which you install separately.
It has no server or state of its own beyond a data directory.

> Status: early, pre-release. See `CHANGELOG.md`.

## Quick Start

> **Requires DeepSeek Harness, installed separately:** `npm install -g @deepseek-ai/dsh`
> (Node.js >= 22.19). dstui does not bundle it.

Linux x86_64 (glibc) only. No Python or other dependencies needed: the installer ships its own.

```bash
curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | sh
```

Installs to `~/.local` by default. Override the prefix, or pin a version, via env:

```bash
curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | sudo DSTUI_PREFIX=/usr/local sh
curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | DSTUI_VERSION=v0.1.0 sh
```

The installed files belong to whoever runs the installer (root for a system install), and
every user can read and run them.

The installer from the GitHub releases is the only way to install dstui: it is not published to
PyPI (its package metadata carries the `Private :: Do Not Upload` classifier, which PyPI refuses).

### Verify the download

`install.sh` downloads with `curl --proto '=https'`, so no redirect can switch the download to
plain HTTP. On a host without curl it falls back to wget, which cannot enforce that;
`DSTUI_VERIFY=1` (below) checks the download either way.

Every release is built by GitHub Actions, which also signs a
[build provenance attestation](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations)
for both release files. With the [GitHub CLI](https://cli.github.com) (logged in), set
`DSTUI_VERIFY=1` and `install.sh` checks the installer's attestation before it runs it. If the
check fails, or `gh` is missing, it stops without running anything:

```bash
curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | DSTUI_VERIFY=1 sh
```

That does not check `install.sh` itself. To check both files by hand:

```bash
curl -fsSLO https://github.com/ksparavec/dstui/releases/latest/download/install.sh
curl -fsSLO https://github.com/ksparavec/dstui/releases/latest/download/dstui-install.sh
gh attestation verify install.sh --repo ksparavec/dstui
gh attestation verify dstui-install.sh --repo ksparavec/dstui
sh dstui-install.sh
```

Then:

```bash
export DEEPSEEK_API_KEY=sk-...
dstui -w ~/src/project     # the agent works in ~/src/project
dstui -w ~/src/project -m deepseek-v4-pro --effort max
```

## Requirements

- **DeepSeek Harness, installed separately:** `npm install -g @deepseek-ai/dsh` (needs
  Node.js >= 22.19). dstui does not bundle it. It runs, in this order: the `--dsh-bin`
  executable, else `dsh` from `PATH`, else the SDK's embedded runtime if that is installed
  (only development and test installs have it: `make dev-install`; the installer does not).
  Without any of them dstui exits with `DeepSeek Harness (dsh) not found`.

  dstui starts that `dsh` as your user, outside the agent's sandbox. Empty and relative `PATH`
  entries are skipped, because they mean the current directory, by default the agent's
  workspace. Any other program named `dsh` on `PATH` is used too, for example the distributed
  shell from Debian's `dsh` package. Pass `--dsh-bin /absolute/path` to pin the right one.
- A DeepSeek API key in `DEEPSEEK_API_KEY`. `DEEPSEEK_BASE_URL` is optional and points the
  agent at a different endpoint. Not used with another provider (see
  [Other providers](#other-providers)).
- Linux x86_64 with glibc (not musl). It is the only platform tested, and the only one the
  installer supports.

The SDK comes from PyPI, following the official DeepSeek Harness install instructions
(`pip install deepseek-harness-sdk`). dstui pins `deepseek-harness-sdk==0.1.5rc1`, the latest
published release. That package depends on `deepseek-harness-runtime-bin`, a 275 MB wheel with
an embedded runtime. The installer leaves it out (it installs the SDK with `--no-deps`); dstui
itself uses it only for its tests, through the `[dev]` extra.

## Install from source

For development. With [uv](https://docs.astral.sh/uv/), which also installs Python 3.14.7, the
exact version in `.python-version`:

```sh
make dev-install                  # .venv with dstui (editable) and the locked development tools
.venv/bin/dstui -w ~/src/project
```

## Usage

Without `-w` the agent works in the current directory. Point it at a project directory, not at
your home directory (see [Profiles](#profiles)). `python -m dstui` also works. Without an API
key the app still starts: it shows a warning, and each prompt then ends with a
`MISSING_CREDENTIAL` error.

| Option | Default | Meaning |
|---|---|---|
| `-w`, `--workspace PATH` | current directory | The agent's working directory. It must exist. With `sdk` the agent can write only inside it. |
| `--profile {sdk,sdk-minimal}` | `sdk` | The agent profile (see below). `sdk-minimal` has no sandbox. |
| `--provider ID` | `deepseek-official` | The model provider. Any other provider must be declared by a `--patch` file. |
| `-m`, `--model MODEL` | `deepseek-v4-flash` | With `deepseek-official`: `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-flash` or `deepseek-v4-flash-vision-exp`. With any other provider: that provider's model id, required. |
| `--effort {off,low,high,max}` | runtime default (`high`) | Reasoning effort. |
| `--max-tokens N` | runtime default | The maximum number of output tokens per model request. |
| `--data-dir PATH` | see [Data](#data) | Where dstui keeps its state. |
| `--dsh-bin PATH` | `dsh` on `PATH` | The DeepSeek Harness executable to run (see [Requirements](#requirements)). |
| `--patch PATH` | none | An extra runtime patch file, applied after dstui's own. Repeatable; applied in order. |
| `--version`, `-h` / `--help` | | Print the version or the help text. |

A leading `~` in `--workspace`, `--data-dir`, `--dsh-bin` and `--patch` is expanded, also in
the `--opt=~/path` form that the shell leaves alone.

## Other providers

The runtime is not tied to DeepSeek's API. A patch file can declare any provider its
`llm-pi-ai` entry supports, such as an OpenAI-compatible server, and `--provider` / `--model`
then select it. For the default `sdk` profile:

```yaml
# local.yml
- id: llm-pi-ai
  config:
    providers:
      local:
        api: openai-completions
        baseURL: http://localhost:8000/v1
        apiKeyEnv: LOCAL_API_KEY       # any non-empty value if the server ignores keys
        models:
          - id: my-model
            contextWindow: 131072
```

```sh
export LOCAL_API_KEY=local
dstui -w ~/src/project --patch local.yml --provider local -m my-model
```

`sdk-minimal` has no `llm-pi-ai` entry, so there `local.yml` changes nothing and the agent
fails to start with `no adapter registered for provider "local"`. For that profile insert the
entry instead, and pass `--profile sdk-minimal --patch local-minimal.yml`. This form fails on
`sdk`, which already has the entry (`duplicate loader entry id`).

```yaml
# local-minimal.yml
- insert:
    - id: llm-pi-ai
      name: '@deepseek-ai/dsh-llm-pi-ai'
      config:
        providers:
          local:
            api: openai-completions
            baseURL: http://localhost:8000/v1
            apiKeyEnv: LOCAL_API_KEY
            models:
              - id: my-model
                contextWindow: 131072
```

A patch entry replaces that entry's whole configuration, so a patch that declares
`llm-pi-ai` replaces every provider declared before it.

With another provider dstui shows no warning about `DEEPSEEK_API_KEY` and hides that key from
the runtime, so `apiKeyEnv` must name another variable. The `sdk` profile's `web_search` tool
calls DeepSeek's search API with that key, so it then fails with `no API key` instead of
sending your queries to DeepSeek.

`--dsh-bin` runs another DeepSeek Harness executable than the `dsh` on `PATH`, for example a
different `dsh` version. dstui still runs it with `DSH_HOME` set to `<data dir>/dsh-home`, so
configuration kept in `~/.dsh` is not read: declare providers with `--patch`.

## Keys

| Key | Action |
|---|---|
| Enter | Send the prompt. The prompt is locked while the agent works. |
| Ctrl+J (or Shift+Enter, if the terminal reports it) | Insert a newline. |
| Escape | Stop the running turn. In the command palette (Ctrl+P) it only closes the palette. |
| Ctrl+N | Start a new conversation. A running turn is stopped first. |
| Ctrl+Q | Quit. The runtime shuts down with the app, even in the middle of a turn. |

Click a `thinking` or `tool: …` row to expand it.

## Profiles

- **`sdk`** (default): the SDK's standard agent, with file editing, a shell, web tools and
  subagents. Its sandbox confines only file **writes** to the workspace. The agent can still
  read any file your account can read (for example `~/.ssh` or the logs of earlier
  conversations) and reach the network, so use it only with content you trust. dstui has no
  approval dialog, so every escalation (approval) request is refused and the tool reports an
  error. This profile loads `AGENTS.md` from the workspace. Each request carries a larger
  system prompt (roughly 8–9k tokens).

  Everything inside the workspace is writable: dotfiles, `.git/hooks` and, if the workspace
  contains it, dstui's own data directory, whose runtime configuration can run code outside
  the sandbox on the next launch. So use a project directory as the workspace, not `$HOME`
  (the default data directory lies under `~/.local/share`).
- **`sdk-minimal`**: a small prompt and one persistent `bash` shell with **no sandbox**. The
  agent can do anything your user account can. Use it only in a workspace and with prompts
  you trust.

## Data

The data directory is `$DSTUI_HOME`. If that is not set, it is `$XDG_DATA_HOME/dstui`, and
otherwise `~/.local/share/dstui`. `--data-dir` overrides all of them. A leading `~` is
expanded, and a relative `$XDG_DATA_HOME` is ignored, as the XDG specification requires.
dstui creates the directories with mode `0700`, so only you can read them. A directory that
already exists keeps its mode.

```text
<data dir>/
  dsh-home/          runtime home (DSH_HOME): session logs in sessions/<workspace>/<session>/
                     and the anonymous install id in .anonymous-user-id
  agents/            DSH_AGENTS_HOME, so skills from ~/.agents stay out of the prompt
  dstui-patch.yml    runtime patch that turns off session-log upload to the model API
  dstui.log          dstui's warnings and errors, with tracebacks
```

dstui also sets `DSH_TELEMETRY_DISABLED=1` for the runtime. Model requests still carry the
session id and an anonymous install id (`dsh-home/.anonymous-user-id`; delete the file to
reset it). Session logs and `dstui.log` are never pruned.

## Limitations

- **No token streaming.** The runtime delivers each model step whole, so replies appear when a
  step finishes. Meanwhile the status bar shows `thinking… Ns`.
- **Stop restarts the runtime and loses the context.** The protocol has no cancel, so Escape
  closes the runtime. The next prompt starts a new conversation in a fresh runtime. A runtime
  crash also starts a new conversation, and the chat says so.
- **No resume.** Every launch starts a new conversation. Old sessions stay only as log files.

## Development

```sh
make dev-install                        # fresh .venv on .python-version: locked [dev] extra + dstui editable
make check                              # ruff (lint + format), mypy --strict and bandit
make test                               # everything
make test PYTEST_ARGS='-m "not e2e"'    # unit and UI tests only (fast, no runtime)
make test PYTEST_ARGS='-m e2e'          # bridge and full-app tests on the real runtime
make test-cov                           # coverage; fails below 90 %
make lock                               # re-pin requirements*.txt after a dependency change
make lock LOCK_ARGS='--upgrade-package textual'   # move one locked package (uv keeps the pins)
make help                               # every target
```

The tests never contact api.deepseek.com and need no API key. The `e2e` tests start a real
runtime and point it at a local fake DeepSeek API (`tests/fake_deepseek.py`). They always use
the SDK's embedded runtime, which only the `[dev]` extra installs
(`deepseek-harness-runtime-bin`); a separately installed `dsh` is never used by the tests. One of them runs `python -m dstui` in a
pseudo-terminal and types into it (`tests/test_pty.py`). A test that leaves a runtime process
running fails.

**Never `/tmp`.** The suite keeps every temp file under `/var/tmp` and removes it when the run
ends, pass or fail (`tests/tmp_hygiene.py`): a full run creates about 100k files, and the
runtime and other child processes inherit its private `TMPDIR`. So plain `uv run pytest` or
`make test` is enough.

`requirements.txt`, `requirements-dev.txt` and `requirements-build.txt` (from `make lock`) are
the locks, fully hashed: what the installer bundles, what `make dev-install` and CI install, and
the build backend that builds the shipped wheel. A test fails when `pyproject.toml` asks for
something the locks do not satisfy. `uv run` works in the `.venv` as it is, without a
`uv.lock`. The tests also fail on any CPython other than `.python-version` (a venv from before a
pin bump): re-run `make dev-install`, which recreates `.venv`. A patch bump of `.python-version`
may need a newer uv, which only knows the CPython patches published before it (in CI too: the
setup-uv `version` in `.github/workflows/release.yml`).

Code layout (`src/dstui/`):

| Module | Role |
|---|---|
| `config.py` | Command line and data directory → `Settings` → SDK config. |
| `events.py` | Pure mapping from runtime notifications to UI events. |
| `bridge.py` | Owns the runtime and the session. Every call blocks, so it runs on worker threads. |
| `widgets.py`, `app.py` | The Textual UI. |
| `__init__.py` | `main()`, the `dstui` entry point. |

## Packaging

`make package` produces `dist/dstui-install.sh`, a single **makeself** self-extracting,
run-once installer (**linux-x86_64**, glibc, ~22 MB). It carries a relocatable CPython
(exactly the X.Y.Z in `.python-version`, now 3.14.7; the build fails on any other) with dstui
and every dependency, **sourceless-precompiled** (`.pyc` only). The interpreter has libpython
linked in statically, so the shared `libpython3.14.so` (32 MB, only for programs that embed
Python) is left out; the build fails if it, or any file that needs it, is in the bundle. The
tree is
**zstd -19** compressed and unpacked at install time by a **bundled static zstd**, so the target
host needs neither Python nor zstd. makeself adds a **SHA256** integrity check. The launcher
runs the bundled Python in isolated mode (`-I`), so `PYTHONPATH`, `PYTHONHOME`, the user site
and the current directory never reach it. Only `dstui` goes on `PATH`.

The payload is owned by `root:root` with no group or other write bits, and the installer
extracts it without restoring owners, so the installed tree belongs to whoever installs and is
readable by everyone. It refuses a prefix with whitespace or one too long for a `#!` line,
resolves a relative prefix against the directory it was started from, checks that the bundled
Python runs on the host before replacing an existing install, and works when its temp directory
is mounted `noexec`. It unpacks under `$TMPDIR`, by default `/var/tmp` (never `/tmp`).

**It does not contain DeepSeek Harness.** `requirements.txt` leaves out
`deepseek-harness-runtime-bin`, the dependencies install hash-checked with `--no-deps`, and
`tools/package/check-no-runtime.sh` fails the build if the runtime, a `dsh`, or anything Node
is in the bundle.

```bash
sh ./dstui-install.sh                                # -> ~/.local
sh ./dstui-install.sh -- --prefix ~/opt/dstui        # or DSTUI_PREFIX=~/opt/dstui
sudo DSTUI_PREFIX=/usr/local sh ./dstui-install.sh   # system install
sh ./dstui-install.sh --check                        # verify integrity only
dstui --help
```

`--target DIR` (without `--`) is makeself's own option: it only unpacks the raw payload into
`DIR`. Use `DSTUI_PREFIX` or `-- --prefix DIR`.

Build deps: `uv`, `makeself`, `curl`, `readelf` (binutils), and a C toolchain (to build the static zstd
once; it is cached under `.cache/`, per version). Run `make lock` and `make dev-install` first.
The dstui wheel is built by the hash-pinned backend of `requirements-build.txt`, and the
dependencies are installed as hash-checked wheels only. All temporary files go to a private
directory under `/var/tmp`. The build fails if the payload holds a build-host path (the
maintainer's home or checkout). It then installs the result into a temporary prefix and checks
it: every module's version against `requirements.txt`, no runtime or Node files, only `dstui`
in `bin/`, `--help`, `--version` (also with a hostile `PYTHONPATH`/`PYTHONHOME`), file modes, a
clear exit 1 without any `dsh`, and one real agent turn against the fake API through
`--dsh-bin`, with the `[dev]` extra's embedded runtime standing in for a separately installed
`dsh` (it is not copied into the bundle).

## Releasing

Releases are built, attested and published by GitHub Actions
(`.github/workflows/release.yml`), because artifact attestations can only be made there.
Maintainers start one with `make release` (`tools/release/release.sh`). It reads the version
from `pyproject.toml`, promotes the `CHANGELOG.md` `[Unreleased]` section to that version,
commits `chore: release vX.Y.Z`, tags `vX.Y.Z` and pushes the commit and the tag together. It
builds nothing and publishes nothing itself.

The pushed tag starts the release workflow. It checks that the tag matches the version in
`pyproject.toml`, builds the installer with `make dev-install` and `make package` (with the full
smoke test), attests `dstui-install.sh` and `install.sh` with `actions/attest-build-provenance`,
and publishes the GitHub release with both files and the version's CHANGELOG section as notes.
`release.sh` follows that run (`gh run watch`) and prints the release URL, or, if the run
fails, how to recover. The same workflow builds the installer (without attesting or publishing)
for every pull request and every push to `main`, so a release build is proven before any tag
exists; only its tag-only publish job may write to the repository or sign.

Before releasing: bump `version` in `pyproject.toml`, add entries under `## [Unreleased]` (an
empty section is refused), and run `make lock` if dependencies changed. Pre-flight guards
require a clean tree on `main` (untracked files and `assume-unchanged` / `skip-worktree`
entries count), in sync with `origin`, with the tag and release not yet present. For
non-interactive runs, set `DSTUI_RELEASE_ASSUME_YES=1` to skip the prompt. The dependency
audit (`pip-audit` of all three locks) runs daily and on every change to a lock; check that its
last run is green before releasing.
