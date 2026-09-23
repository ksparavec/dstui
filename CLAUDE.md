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
