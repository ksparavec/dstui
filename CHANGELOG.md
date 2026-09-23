# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Chat with a DeepSeek Harness agent in the terminal.** A Textual UI on the
  official Python SDK (`deepseek-harness-sdk` 0.1.5rc1): replies render as
  Markdown, the agent's reasoning and tool calls appear as collapsible blocks,
  and a status bar shows what the agent is doing, the model and profile, elapsed
  time, retries and token totals.
- **Keys:** Enter sends, Ctrl+J (or Shift+Enter) inserts a newline, Escape stops
  the running turn, Ctrl+N starts a new conversation, Ctrl+Q quits. The runtime
  always shuts down with the app, even mid-turn.
- **Options:** `-w/--workspace`, `--profile {sdk,sdk-minimal}`, `-m/--model`,
  `--effort {off,low,high,max}`, `--max-tokens`, `--data-dir`, `--version`.
- **Other providers:** `--provider ID` with a free-form `-m/--model`, declared to
  the runtime by repeatable `--patch PATH` files (e.g. an OpenAI-compatible
  server). With any provider other than `deepseek-official`, `DEEPSEEK_API_KEY`
  is hidden from the runtime, so the `sdk` profile's `web_search` cannot send it
  (or your queries) to DeepSeek.
- **Runs your DeepSeek Harness:** dstui launches `--dsh-bin PATH`, else `dsh`
  from `PATH`, and exits with `DeepSeek Harness (dsh) not found` when there is
  neither. DeepSeek Harness is not part of dstui: install it separately with
  `npm install -g @deepseek-ai/dsh` (Node.js >= 22.19).
- **Private state:** everything lives in one data directory (`$DSTUI_HOME`,
  `$XDG_DATA_HOME/dstui` or `~/.local/share/dstui`) created with mode `0700`.
  Session-log upload to the model API and runtime telemetry are turned off;
  warnings and errors go to `dstui.log` there.
- **Self-contained installer** for Linux x86_64: one download that brings its
  own Python, so the host needs none. Install with
  `curl -fsSL https://github.com/ksparavec/dstui/releases/latest/download/install.sh | sh`
  (`DSTUI_PREFIX` picks the prefix, default `~/.local`; `DSTUI_VERSION` pins a
  release). It does not include DeepSeek Harness.

