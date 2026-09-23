"""Shared fixtures: a fake DeepSeek API and isolated configs for the real SDK runtime."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from deepseek_harness import DeepSeekHarnessConfig, Notification

from tests.fake_deepseek import FakeDeepSeek

TRACES_DIR = Path(__file__).parent / "fixtures" / "traces"
LEAK_GRACE_SECONDS = 3.0


def load_trace(name: str) -> list[Notification]:
    """Notifications captured from the real 0.1.5rc1 runtime (``v017-*`` from 0.1.7a2)."""
    lines = (TRACES_DIR / name).read_text().splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    return [Notification(record["method"], record["payload"]) for record in records]


@pytest.fixture(scope="session")
def fake_api() -> Iterator[FakeDeepSeek]:
    with FakeDeepSeek() as fake:
        yield fake


@pytest.fixture
def fake(fake_api: FakeDeepSeek) -> FakeDeepSeek:
    """The shared fake API with an empty script. Unscripted requests get HTTP 500."""
    fake_api.reset()
    fake_api.set_default(None)
    fake_api.set_responder(None)
    return fake_api


@pytest.fixture
def no_retry_patch(tmp_path: Path) -> Path:
    """Runtime patch that disables provider retries, so error turns end in milliseconds."""
    path = tmp_path / "no-retry.patch.yml"
    rows = [{"id": "llm-deepseek", "config": {"retryPolicy": {"mode": "normal", "maxRetries": 0}}}]
    path.write_text(json.dumps(rows))  # JSON is valid YAML
    return path


type HarnessConfigFactory = Callable[..., DeepSeekHarnessConfig]


@pytest.fixture
def make_harness_config(
    tmp_path: Path, fake: FakeDeepSeek, no_retry_patch: Path
) -> HarnessConfigFactory:
    """Build an SDK config pointing at the fake API with an isolated DSH_HOME and workspace.

    Always sets api_key and base_url so a real key or endpoint is never inherited.
    """

    def factory(
        profile: str = "sdk-minimal",
        *,
        retries: bool = False,
        patches: tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        **overrides: object,
    ) -> DeepSeekHarnessConfig:
        workspace = tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        base_patches = () if retries else (str(no_retry_patch),)
        return DeepSeekHarnessConfig(
            profile=profile,
            model="deepseek-v4-flash",
            api_key="sk-fake",
            base_url=fake.url,
            cwd=str(workspace),
            dsh_home=str(tmp_path / "dsh-home"),
            patches=(*base_patches, *patches),
            env={
                "DSH_TELEMETRY_DISABLED": "1",
                "DSH_AGENTS_HOME": str(tmp_path / "agents"),
                **(env or {}),
            },
            shutdown_timeout_seconds=1.0,
            **overrides,
        )

    return factory


@pytest.fixture(autouse=True)
def _no_leaked_runtimes(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail an ``e2e`` test that leaves a runtime process running."""
    if request.node.get_closest_marker("e2e") is None:
        yield
        return
    tmp_path: Path = request.getfixturevalue("tmp_path")  # set up first, so torn down after us
    yield
    deadline = time.monotonic() + LEAK_GRACE_SECONDS  # children (e.g. the shell) exit async
    while (leaked := runtime_pids_under(tmp_path)) and time.monotonic() < deadline:
        time.sleep(0.05)
    described = [f"{pid}: {_cmdline(pid)}" for pid in leaked]
    for pid in leaked:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    assert not leaked, f"runtime processes leaked: {described}"


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()[:200]
    except OSError:
        return "?"


def runtime_pids_under(root: Path) -> list[int]:
    """PIDs of runtime processes whose DSH_HOME lies under ``root``."""
    prefix = f"DSH_HOME={root}".encode()
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if any(item.startswith(prefix) for item in environ.split(b"\0")):
            pids.append(int(entry.name))
    return pids
