"""The whole app end to end: DsTuiApp + AgentBridge + the real SDK runtime + the fake API.

Driven only through the UI: keys typed into ``#prompt``, then Enter / Escape / Ctrl+N / Ctrl+Q.
"""

from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from textual.pilot import Pilot

from dstui.app import DsTuiApp
from dstui.bridge import AgentBridge
from dstui.config import build_harness_config, parse_args
from dstui.widgets import (
    AssistantMessage,
    Notice,
    PromptArea,
    ReasoningBlock,
    ToolCallBlock,
    UserMessage,
)
from tests import helpers_ui
from tests.conftest import runtime_pids_under
from tests.fake_deepseek import (
    FakeDeepSeek,
    JsonObject,
    auth_error_reply,
    conversation,
    text_reply,
    tool_call_reply,
)
from tests.helpers_ui import RESTARTED, SIZE, STOPPED, chat_items, notices, status

pytestmark = pytest.mark.e2e

WAIT_S = 20.0  # the real runtime boots and answers: allow much longer than the UI tests
QUIT_S = 5.0  # quitting mid-turn was measured at ~0.1 s; the bound only has to catch "hangs"
wait_until = partial(helpers_ui.wait_until, timeout=WAIT_S)
MARKDOWN = "**Hello** from the _fake_ API.\n\n- one\n- two"


def build_app(
    tmp_path: Path,
    fake: FakeDeepSeek,
    no_retry_patch: Path,
    *,
    profile: str = "sdk-minimal",
    api_key: str = "sk-fake",
) -> DsTuiApp:
    """The app exactly as ``dstui.main`` builds it, but pointed at the fake API."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    argv = ["-w", str(workspace), "--data-dir", str(tmp_path / "data"), "--profile", profile]
    settings = parse_args(argv, env={"DEEPSEEK_API_KEY": api_key})
    base = build_harness_config(settings)
    config = replace(
        base,
        api_key=api_key,
        base_url=fake.url,
        patches=(*base.patches, str(no_retry_patch)),
        env={**base.env},
    )
    return DsTuiApp(AgentBridge(config), settings)


@pytest.fixture
def make_app(tmp_path: Path, fake: FakeDeepSeek, no_retry_patch: Path) -> Callable[..., DsTuiApp]:
    def factory(**options: str) -> DsTuiApp:
        return build_app(tmp_path, fake, no_retry_patch, **options)

    return factory


def replies(app: DsTuiApp) -> list[str]:
    return [message.source for message in chat_items(app, AssistantMessage)]


def dialogue(body: JsonObject) -> list[tuple[str, str]]:
    return [(role, text) for role, text in conversation(body) if role != "system"]


def user_texts(body: JsonObject) -> str:
    return "\n".join(text for role, text in dialogue(body) if role == "user")


async def send(pilot: Pilot[None], text: str) -> None:
    """Type ``text`` into the focused prompt and press Enter; wait for the local echo."""
    app = pilot.app
    prompt = app.query_one("#prompt", PromptArea)
    await wait_until(pilot, lambda: not prompt.disabled and app.focused is prompt)
    echoed = len(chat_items(app, UserMessage))
    await pilot.press(*text, "enter")
    await wait_until(pilot, lambda: len(chat_items(app, UserMessage)) == echoed + 1)
    assert chat_items(app, UserMessage)[-1].text == text


async def turn_ended(pilot: Pilot[None]) -> None:
    app = pilot.app
    prompt = app.query_one("#prompt", PromptArea)
    await wait_until(pilot, lambda: not prompt.disabled and status(app).startswith("ready"))


async def test_plain_reply_renders_markdown_title_and_token_totals(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply(MARKDOWN))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(pilot, lambda: status(app).startswith("ready"))
        assert app.sub_title == "new conversation"
        await send(pilot, "hello there")
        await turn_ended(pilot)
        assert replies(app) == [MARKDOWN]
        assert app.sub_title == "hello there"
        assert "deepseek-v4-flash · sdk-minimal" in status(app)
        assert "tokens in 11 · out 7" in status(app)
        assert notices(app, "error") == [] and notices(app, "warning") == []
    assert dialogue(fake.requests[0]) == [("user", "hello there")]


async def test_reasoning_reply_renders_a_collapsed_reasoning_block_first(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("The answer is 42.", reasoning="the user wants the answer"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "what is the answer")
        await turn_ended(pilot)
        [block] = chat_items(app, ReasoningBlock)
        assert block.text == "the user wants the answer"
        assert block.collapsed
        assert replies(app) == ["The answer is 42."]
        kinds = [type(child) for child in app.query_one("#chat").children]
        assert kinds == [UserMessage, ReasoningBlock, AssistantMessage]


async def test_tool_turn_renders_the_real_bash_output(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(tool_call_reply("bash", {"command": "echo hi"}), text_reply("It printed hi."))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "run echo hi")
        await turn_ended(pilot)
        [block] = chat_items(app, ToolCallBlock)
        assert block.tool_name == "bash"
        assert json.loads(block.arguments) == {"command": "echo hi"}
        assert block.output is not None
        assert block.output.splitlines()[0] == "hi"
        assert not block.is_error
        assert replies(app) == ["It printed hi."]
    assert len(fake.requests) == 2
    assert any(role == "tool" and "hi" in text for role, text in dialogue(fake.requests[1]))


async def test_second_turn_keeps_the_conversation_history(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("first answer"), text_reply("second answer"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "first question")
        await turn_ended(pilot)
        await send(pilot, "second question")
        await turn_ended(pilot)
        assert replies(app) == ["first answer", "second answer"]
        assert "tokens in 22 · out 14" in status(app)
    assert dialogue(fake.requests[1]) == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
    ]


async def test_auth_error_shows_a_hint_and_the_next_prompt_works(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(auth_error_reply(), text_reply("recovered"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "hello")
        await turn_ended(pilot)
        [error] = notices(app, "error")
        assert error.startswith("model error AUTH: Authentication Fails")
        assert error.endswith("(the API key was rejected)")
        assert replies(app) == []
        await send(pilot, "again")
        await turn_ended(pilot)
        assert replies(app) == ["recovered"]
    user_side = user_texts(fake.requests[1])  # the failed prompt stays in the history
    assert "hello" in user_side and "again" in user_side


async def test_truncated_reply_shows_the_partial_text_and_a_warning(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("partial answer", finish="length"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "write a long essay")
        await turn_ended(pilot)
        assert replies(app) == ["partial answer"]
        assert notices(app, "warning") == [
            "reply truncated: the model reached its max-tokens limit"
        ]
        assert notices(app, "error") == []


async def test_escape_stops_the_turn_and_the_next_prompt_starts_fresh(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("slow " * 40, chunks=40, chunk_delay_s=0.25), text_reply("fresh"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "long task")
        await wait_until(pilot, lambda: len(fake.recorded) >= 1)  # the model request is open
        await wait_until(pilot, lambda: app.sub_title == "long task")
        await pilot.press("escape")
        await wait_until(pilot, lambda: notices(app, "info") == [STOPPED], timeout=5.0)
        await turn_ended(pilot)
        assert app.sub_title == "new conversation"
        assert replies(app) == []
        await send(pilot, "next")
        await turn_ended(pilot)
        assert replies(app) == ["fresh"]
    assert len(fake.requests) == 2
    assert dialogue(fake.requests[1]) == [("user", "next")]


async def test_runtime_crash_mid_turn_says_the_context_is_lost_and_the_next_prompt_works(
    tmp_path: Path, make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    slow = text_reply("slow " * 40, chunks=40, chunk_delay_s=0.25)
    fake.enqueue(text_reply("first answer"), slow, text_reply("after the crash"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "first question")
        await turn_ended(pilot)
        assert app.sub_title == "first question"
        await send(pilot, "doomed")
        await wait_until(pilot, lambda: len(fake.recorded) >= 2)  # the model request is open
        for pid in runtime_pids_under(tmp_path):
            os.kill(pid, signal.SIGKILL)
        await turn_ended(pilot)
        [error] = notices(app, "error")
        assert error.startswith("agent runtime error: ")
        assert notices(app, "info") == [RESTARTED]
        assert app.sub_title == "new conversation"
        await send(pilot, "follow-up")
        await turn_ended(pilot)
        assert replies(app) == ["first answer", "after the crash"]
        assert app.sub_title == "follow-up"
    assert dialogue(fake.requests[-1]) == [("user", "follow-up")]  # a fresh runtime session


async def test_ctrl_n_clears_the_chat_and_the_next_request_has_no_history(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("old answer"), text_reply("new answer"))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "old topic")
        await turn_ended(pilot)
        assert app.sub_title == "old topic"
        await pilot.press("ctrl+n")
        await wait_until(pilot, lambda: notices(app, "info") == ["new conversation"])
        assert [type(child) for child in app.query_one("#chat").children] == [Notice]
        assert app.sub_title == "new conversation"
        assert "tokens in 0 · out 0" in status(app)
        await send(pilot, "new topic")
        await turn_ended(pilot)
        assert replies(app) == ["new answer"]
        assert app.sub_title == "new topic"
    assert dialogue(fake.requests[1]) == [("user", "new topic")]


async def test_ctrl_q_mid_turn_exits_promptly_and_reaps_the_runtime(
    tmp_path: Path, make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("slow " * 40, chunks=40, chunk_delay_s=0.25))
    app = make_app()
    async with app.run_test(size=SIZE) as pilot:
        await send(pilot, "long task")
        await wait_until(pilot, lambda: len(fake.recorded) >= 1)
        assert runtime_pids_under(tmp_path) != []
        quit_pressed = time.monotonic()
        await pilot.press("ctrl+q")
    assert time.monotonic() - quit_pressed < QUIT_S
    assert app.return_code == 0
    assert runtime_pids_under(tmp_path) == []


async def test_default_sdk_profile_answers_a_plain_prompt(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("hello from the sandbox"))
    app = make_app(profile="sdk")
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(pilot, lambda: status(app).startswith("ready"))
        assert "deepseek-v4-flash · sdk │" in status(app)
        await send(pilot, "hi")
        await turn_ended(pilot)
        assert replies(app) == ["hello from the sandbox"]
        assert notices(app, "error") == []
    assert "hi" in user_texts(fake.requests[0])


async def test_missing_api_key_warns_at_startup_and_a_prompt_explains_it(
    make_app: Callable[..., DsTuiApp], fake: FakeDeepSeek
) -> None:
    app = make_app(api_key="")
    assert app.settings.api_key_set is False
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(pilot, lambda: status(app).startswith("ready"))
        [warning] = notices(app, "warning")
        assert "DEEPSEEK_API_KEY is not set" in warning
        await send(pilot, "hello")
        await turn_ended(pilot)
        [error] = notices(app, "error")
        assert error.startswith("model error MISSING_CREDENTIAL")
        assert error.endswith("(set DEEPSEEK_API_KEY and restart dstui)")
    assert fake.recorded == []  # the runtime never calls the API without a key
