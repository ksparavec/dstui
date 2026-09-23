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

Linux x86_64 only. No Python or other dependencies needed: the installer ships its own.

```bash
curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | sh
```

Installs to `~/.local` by default. Override the prefix, or pin a version, via env:

```bash
curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | DSTUI_PREFIX=/usr/local sh
curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | DSTUI_VERSION=v0.1.0 sh
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
  (only development and test installs have it). Without any of them dstui exits with
  `DeepSeek Harness (dsh) not found`.
- A DeepSeek API key in `DEEPSEEK_API_KEY`. `DEEPSEEK_BASE_URL` is optional and points the
  agent at a different endpoint. Not used with another provider (see
  [Other providers](#other-providers)).
- Linux x86_64. It is the only platform tested, and the only one the installer supports.

The SDK comes from PyPI, following the official DeepSeek Harness install instructions
(`pip install deepseek-harness-sdk`). dstui pins `deepseek-harness-sdk==0.1.5rc1`, the latest
published release. That package depends on `deepseek-harness-runtime-bin`, a 275 MB wheel with
an embedded runtime. The installer leaves it out (it installs the SDK with `--no-deps`); dstui
itself uses it only for its tests. A plain `pip install` of dstui still pulls it in through the
SDK.

## Install from source

With [uv](https://docs.astral.sh/uv/), which also installs Python 3.14:

```sh
make dev-install                  # .venv with dstui (editable) and the development tools
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
make dev-install                        # .venv: dstui editable + the [dev] extra
make check                              # ruff (lint + format) and mypy --strict
make test                               # everything
make test PYTEST_ARGS='-m "not e2e"'    # unit and UI tests only (fast, no runtime)
make test PYTEST_ARGS='-m e2e'          # bridge and full-app tests on the real runtime
make test-cov                           # coverage; fails below 90 %
make lock                               # re-pin requirements*.txt after a dependency change
make help                               # every target
```

The tests never contact api.deepseek.com and need no API key. The `e2e` tests start a real
runtime and point it at a local fake DeepSeek API (`tests/fake_deepseek.py`). With no
separately installed `dsh` they use the SDK's embedded runtime, which only the `[dev]` extra
installs (`deepseek-harness-runtime-bin`). One of them runs `python -m dstui` in a
pseudo-terminal and types into it (`tests/test_pty.py`). A test that leaves a runtime process
running fails.

**Never `/tmp`.** `make test` and `make test-cov` give pytest a private `TMPDIR` and
`--basetemp` under `/var/tmp` and delete both afterwards, pass or fail: a full run leaves about
100k files, and the runtime leaves its own scratch directories in `$TMPDIR`. Running pytest
directly, do the same:

```sh
export TMPDIR=/var/tmp/dstui-$USER-tmp && mkdir -p "$TMPDIR"
uv run pytest --basetemp=/var/tmp/dstui-$USER-pytest -m "not e2e"
rm -rf /var/tmp/dstui-$USER-pytest "$TMPDIR"
```

`requirements.txt` and `requirements-dev.txt` (from `make lock`) are the locks. `uv run` works in
the `.venv` as it is, without a `uv.lock`.

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
run-once installer (**linux-x86_64**, ~31 MB). It carries a relocatable CPython 3.14 with dstui
and every dependency, **sourceless-precompiled** (`.pyc` only). The tree is **zstd -19**
compressed and unpacked at install time by a **bundled static zstd**, so the target host needs
neither Python nor zstd. makeself adds a **SHA256** integrity check, and the `-s` launcher
keeps it hermetic (ignores the host user site). Only `dstui` goes on `PATH`.

**It does not contain DeepSeek Harness.** `requirements.txt` leaves out
`deepseek-harness-runtime-bin`, the dependencies install hash-checked with `--no-deps`, and
`tools/package/check-no-runtime.sh` fails the build if the runtime, a `dsh`, or anything Node
is in the bundle.

```bash
./dstui-install.sh                           # -> ~/.local
DSTUI_PREFIX=/usr/local ./dstui-install.sh   # system install
sh ./dstui-install.sh --check                # verify integrity only
dstui --help
```

Build deps: `uv`, `makeself`, `curl`, and a C toolchain (to build the static zstd once; it is
cached under `.cache/`). Run `make lock` and `make dev-install` first. All temporary files go to
a private directory under `/var/tmp`. The build then installs the result into a temporary
prefix and checks it: every module's version against `requirements.txt`, no runtime or Node
files, only `dstui` in `bin/`, `--help`, `--version`, a clear exit 1 without any `dsh`, and one
real agent turn against the fake API through `--dsh-bin`, with the `[dev]` extra's embedded
runtime standing in for a separately installed `dsh` (it is not copied into the bundle).

## Releasing

Maintainers cut a release with `make release`. It reads the version from `pyproject.toml`,
promotes the `CHANGELOG.md` `[Unreleased]` section to that version, rebuilds the installer, tags
`vX.Y.Z`, pushes, and publishes a GitHub release with two assets: the `dstui-install.sh` bundle
and the `install.sh` bootstrap behind the one-liner in the [Quick Start](#quick-start).

Before releasing: bump `version` in `pyproject.toml`, add entries under `## [Unreleased]` (an
empty section is refused), and run `make lock` if dependencies changed. Pre-flight guards
require a clean tree on `main`, in sync with `origin`, with the tag and release not yet present.
For CI or other non-interactive runs, set `DSTUI_RELEASE_ASSUME_YES=1` to skip the prompt.
