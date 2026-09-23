"""Another provider, declared by the README's own --patch examples, on the real runtime.

dstui's path (``parse_args`` -> ``build_harness_config`` -> ``AgentBridge``) talks to the fake
API's OpenAI-compatible ``/v1/chat/completions``. A second fake stands in for DeepSeek's
endpoints, with a DeepSeek key exported as a user of both would have it: nothing may reach it.
"""

from __future__ import annotations

import re
import socket
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from deepseek_harness_runtime import resolve_bundled_launch_args

from dstui.app import describe_turn_error
from dstui.bridge import AgentBridge, TurnOutcome
from dstui.config import Settings, build_harness_config, parse_args
from dstui.events import AssistantText, ToolFinished, TurnFinished, UiEvent
from tests.fake_deepseek import FakeDeepSeek, text_reply, tool_call_reply

pytestmark = pytest.mark.e2e

README = Path(__file__).parents[1] / "README.md"
README_URL = "http://localhost:8000/v1"  # the baseURL in the README's examples
KEY_ENV = "LOCAL_API_KEY"  # the README's apiKeyEnv
DEEPSEEK_KEY = "sk-deepseek-secret"

type Run = Callable[..., tuple[Settings, AgentBridge]]


def readme_patch(name: str, path: Path, base_url: str) -> Path:
    """Write the README's ``# <name>`` YAML example to ``path``, pointed at ``base_url``."""
    blocks = re.findall(r"```yaml\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
    [block] = [block for block in blocks if block.startswith(f"# {name}\n")]
    assert README_URL in block
    path.write_text(block.replace(README_URL, base_url), encoding="utf-8")
    return path


def dead_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"  # nothing listens once the socket is closed


@pytest.fixture
def deepseek(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDeepSeek]:
    """DeepSeek's chat and search endpoints, and its key: the other provider must not use them."""
    with FakeDeepSeek() as fake_deepseek:
        fake_deepseek.set_default(text_reply("from DeepSeek"))
        monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_KEY)
        monkeypatch.setenv("DEEPSEEK_BASE_URL", fake_deepseek.url)
        monkeypatch.setenv("DEEPSEEK_SEARCH_BASE_URL", f"{fake_deepseek.url}/v1")
        monkeypatch.setenv(KEY_ENV, "local")
        yield fake_deepseek
        assert fake_deepseek.recorded == []


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    """``run(*argv) -> (settings, bridge)``: the README's command, with a private data dir."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridges: list[AgentBridge] = []

    def factory(*argv: str) -> tuple[Settings, AgentBridge]:
        common = ["-w", str(workspace), "--data-dir", str(tmp_path / "data")]
        settings = parse_args([*common, "--provider", "local", "-m", "my-model", *argv], env={})
        config = build_harness_config(settings)
        config.shutdown_timeout_seconds = 1.0
        bridges.append(AgentBridge(config))
        return settings, bridges[-1]

    yield factory
    for bridge in bridges:
        bridge.close()


@pytest.mark.usefixtures("deepseek")
@pytest.mark.parametrize(
    ("profile", "patch_name"), [("sdk", "local.yml"), ("sdk-minimal", "local-minimal.yml")]
)
def test_the_readme_patch_for_each_profile_completes_a_turn(
    profile: str, patch_name: str, run: Run, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(text_reply("hello from the local model"))
    patch = readme_patch(patch_name, tmp_path / patch_name, f"{fake.url}/v1")
    events: list[UiEvent] = []

    _, bridge = run("--profile", profile, "--patch", str(patch))
    outcome = bridge.send("hello", events.append)

    assert outcome == TurnOutcome("completed")
    assert AssistantText("hello from the local model") in events
    [request] = fake.recorded
    assert request.path == "/v1/chat/completions"
    assert request.body is not None and request.body["model"] == "my-model"
    assert request.headers["authorization"] == "Bearer local"  # apiKeyEnv, not DeepSeek's key


@pytest.mark.usefixtures("deepseek")
def test_a_later_patch_replaces_the_provider_of_an_earlier_one(
    run: Run, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(text_reply("the second patch won"))
    first = readme_patch("local.yml", tmp_path / "first.yml", dead_url())
    second = readme_patch("local.yml", tmp_path / "second.yml", f"{fake.url}/v1")

    _, bridge = run("--patch", str(first), "--patch", str(second))

    assert bridge.send("hello", [].append) == TurnOutcome("completed")
    assert len(fake.recorded) == 1


@pytest.mark.usefixtures("deepseek")
def test_web_search_does_not_send_the_query_or_the_deepseek_key_to_deepseek(
    run: Run, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    """The sdk profile's web_search is DeepSeek's search API: with another provider it fails."""
    fake.enqueue(
        tool_call_reply("web_search", {"queries": ["PRIVATE-CODENAME internals"]}),
        text_reply("no search then"),
    )
    patch = readme_patch("local.yml", tmp_path / "local.yml", f"{fake.url}/v1")
    events: list[UiEvent] = []

    _, bridge = run("--patch", str(patch))
    outcome = bridge.send("search the web", events.append)

    assert outcome == TurnOutcome("completed")
    [finished] = [event for event in events if isinstance(event, ToolFinished)]
    assert finished.is_error
    assert "no API key" in finished.output  # and the fixture checks DeepSeek got no request


@pytest.mark.usefixtures("deepseek")
def test_a_missing_provider_key_is_named_without_a_deepseek_hint(
    run: Run, fake: FakeDeepSeek, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY_ENV)
    patch = readme_patch("local.yml", tmp_path / "local.yml", f"{fake.url}/v1")
    events: list[UiEvent] = []

    settings, bridge = run("--patch", str(patch))
    bridge.send("hello", events.append)

    [finished] = [event for event in events if isinstance(event, TurnFinished)]
    assert finished.code == "MISSING_CREDENTIAL"
    shown = describe_turn_error(
        finished.code, finished.message, deepseek=settings.needs_deepseek_key
    )
    assert KEY_ENV in shown
    assert "DEEPSEEK_API_KEY" not in shown
    assert fake.recorded == []


@pytest.mark.usefixtures("deepseek")
def test_dsh_bin_runs_that_executable_with_dstuis_patch_then_the_extra_ones(
    run: Run, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    [runtime] = resolve_bundled_launch_args()
    argv_log = tmp_path / "argv.log"
    wrapper = tmp_path / "dsh"
    wrapper.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_log}"\nexec "{runtime}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    fake.enqueue(text_reply("via the wrapper"))
    patch = readme_patch("local.yml", tmp_path / "local.yml", f"{fake.url}/v1")

    settings, bridge = run("--dsh-bin", str(wrapper), "--patch", str(patch))

    assert bridge.send("hello", [].append) == TurnOutcome("completed")
    assert argv_log.read_text().splitlines() == [
        "--profile", "sdk", "--patch", str(settings.patch_file), "--patch", str(patch),
    ]  # fmt: skip
