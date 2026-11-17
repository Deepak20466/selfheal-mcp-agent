"""Offline tests for the Claude CLI benchmark provider.

No test runs the real `claude` binary, makes a model request, or reads credentials.
Subprocess behaviour is exercised with small fake executables written into tmp_path.
Run with --noconftest so the project's database-backed fixtures are not started:

    python -m pytest --noconftest -p no:cacheprovider tests/test_claude_cli_provider.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from benchmarks.real_bugs import claude_cli_provider as cli
from benchmarks.real_bugs import unresolved_eval as ue

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = [
    {"name": "read_file", "description": "read", "input_schema": {"type": "object"}},
    {"name": "run_tests", "description": "test", "input_schema": {"type": "object"}},
    {"name": "propose_patch", "description": "patch", "input_schema": {"type": "object"}},
]
TOOL_NAMES = [tool["name"] for tool in TOOLS]


def _fake_executable(path: Path, body: str) -> Path:
    path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------- schema and transcript


def test_schema_constrains_tool_names_to_the_offered_set() -> None:
    schema = cli.build_tool_call_schema(TOOL_NAMES)
    item = schema["properties"]["tool_calls"]["items"]
    assert item["properties"]["name"]["enum"] == sorted(TOOL_NAMES)
    assert item["required"] == ["name", "arguments_json"]
    assert item["additionalProperties"] is False
    assert schema["required"] == ["text", "tool_calls"]


def test_schema_refuses_an_empty_tool_list() -> None:
    with pytest.raises(ValueError):
        cli.build_tool_call_schema([])


def test_transcript_contains_system_tools_history_and_truncated_results() -> None:
    long_result = "x" * (cli.TOOL_RESULT_MAX_CHARS + 500)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Fix the fee balance."},
        {
            "role": "assistant",
            "content": [
                cli.Block(type="text", text="Reading the file."),
                cli.Block(type="tool_use", id="c1", name="read_file", input={"path": "a.py"}),
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "c1",
                    "content": long_result,
                    "is_error": False,
                }
            ],
        },
    ]
    prompt = cli.render_transcript("SYSTEM TEXT", messages, TOOLS)
    assert "SYSTEM TEXT" in prompt
    assert '"name": "propose_patch"' in prompt
    assert "[ASSISTANT TOOL CALL id=c1 name=read_file]" in prompt
    assert "[TOOL RESULT for id=c1 is_error=false]" in prompt
    assert "[truncated 500 characters]" in prompt
    assert ("x" * cli.TOOL_RESULT_MAX_CHARS) in prompt
    assert ("x" * (cli.TOOL_RESULT_MAX_CHARS + 1)) not in prompt


def test_transcript_rejects_unknown_block_types() -> None:
    messages = [{"role": "assistant", "content": [{"type": "image", "source": "x"}]}]
    with pytest.raises(cli.ClaudeCLIProviderError):
        cli.render_transcript("", messages, TOOLS)


def test_system_text_accepts_string_and_text_blocks() -> None:
    assert cli.system_text("plain") == "plain"
    assert (
        cli.system_text([{"type": "text", "text": "a"}, cli.Block(type="text", text="b")]) == "a\nb"
    )
    assert cli.system_text(None) == ""


# ---------------------------------------------------------------- parsing and fail-closed


def _payload(structured: Any, **extra: Any) -> str:
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": structured,
        "num_turns": 2,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 7,
        },
        "total_cost_usd": 0.42,
    }
    body.update(extra)
    return json.dumps(body)


def test_parse_accepts_a_valid_tool_call_and_records_unverified_estimate() -> None:
    structured = {
        "text": "reading",
        "tool_calls": [{"name": "read_file", "arguments_json": '{"path": "app/routers/fees.py"}'}],
    }
    turn = cli.parse_turn(_payload(structured), "", 0, TOOL_NAMES)
    assert turn.tool_calls == (("read_file", {"path": "app/routers/fees.py"}),)
    assert turn.text == "reading"
    assert (turn.input_tokens, turn.output_tokens) == (10, 5)
    assert (turn.cache_creation_input_tokens, turn.cache_read_input_tokens) == (3, 7)
    assert turn.reported_estimate_usd == "0.42"


def test_parse_accepts_an_empty_tool_list_as_a_final_answer() -> None:
    turn = cli.parse_turn(_payload({"text": "done", "tool_calls": []}), "", 0, TOOL_NAMES)
    assert turn.tool_calls == ()
    assert turn.text == "done"


def test_parse_falls_back_to_result_text_when_structured_output_is_absent() -> None:
    result = json.dumps({"text": "t", "tool_calls": []})
    stdout = json.dumps({"is_error": False, "result": result, "usage": {}})
    turn = cli.parse_turn(stdout, "", 0, TOOL_NAMES)
    assert turn.text == "t"


@pytest.mark.parametrize(
    "structured",
    [
        {"text": "", "tool_calls": [{"name": "delete_everything", "arguments_json": "{}"}]},
        {"text": "", "tool_calls": [{"name": "read_file", "arguments_json": "not json"}]},
        {"text": "", "tool_calls": [{"name": "read_file", "arguments_json": "[1, 2]"}]},
        {"text": "", "tool_calls": [{"name": "read_file", "arguments_json": 5}]},
        {"text": 3, "tool_calls": []},
        {"text": "", "tool_calls": "nope"},
        {"text": "", "tool_calls": ["read_file"]},
        None,
    ],
)
def test_parse_fails_closed_on_malformed_or_unoffered_output(structured: Any) -> None:
    with pytest.raises(cli.ClaudeCLIMalformedOutputError):
        cli.parse_turn(_payload(structured), "", 0, TOOL_NAMES)


def test_parse_rejects_non_json_stdout() -> None:
    with pytest.raises(cli.ClaudeCLIMalformedOutputError):
        cli.parse_turn("plain text", "", 1, TOOL_NAMES)


def test_parse_maps_usage_limit_to_its_own_error() -> None:
    stdout = json.dumps({"is_error": True, "result": "Claude usage limit reached. Resets at 5pm."})
    with pytest.raises(cli.ClaudeCLIUsageLimitError):
        cli.parse_turn(stdout, "", 1, TOOL_NAMES)


def test_parse_maps_not_logged_in_to_auth_error() -> None:
    stdout = json.dumps({"is_error": True, "result": "Not logged in. Please run /login."})
    with pytest.raises(cli.ClaudeCLIAuthError):
        cli.parse_turn(stdout, "", 1, TOOL_NAMES)


def test_parse_reports_generic_cli_errors_without_guessing() -> None:
    stdout = json.dumps({"is_error": True, "result": "something unexpected"})
    with pytest.raises(cli.ClaudeCLIProviderError):
        cli.parse_turn(stdout, "", 0, TOOL_NAMES)


# ---------------------------------------------------------------- environment and argv


def test_env_removes_api_key_and_endpoint_overrides_only() -> None:
    base = {
        "ANTHROPIC_API_KEY": "sk-test",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "ANTHROPIC_MODEL": "m",
        "HOME": "/home/x",
        "PATH": "/usr/bin",
    }
    env = cli.build_cli_env(base)
    assert env == {"HOME": "/home/x", "PATH": "/usr/bin"}


def test_argv_disables_tools_sessions_and_mcp_and_never_uses_bare_mode(tmp_path: Path) -> None:
    schema = cli.build_tool_call_schema(TOOL_NAMES)
    argv = cli.build_cli_argv(
        binary="/bin/claude",
        model="claude-sonnet-5-5",
        schema=schema,
        mcp_config=tmp_path / "m.json",
    )
    assert argv[0] == "/bin/claude"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert "--bare" not in argv
    assert not any("api" in part.lower() and "key" in part.lower() for part in argv)


# ---------------------------------------------------------------- subscription gate


GOOD_STATUS = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "subscriptionType": "pro",
    "apiProvider": "firstParty",
    "email": "person@example.invalid",
    "orgId": "should-not-leak",
}


def test_login_gate_accepts_only_claude_ai_pro_and_returns_safe_fields() -> None:
    summary = cli.verify_subscription_login(GOOD_STATUS)
    assert summary == {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "subscriptionType": "pro",
        "apiProvider": "firstParty",
    }
    assert "email" not in summary and "orgId" not in summary


@pytest.mark.parametrize(
    "override, field",
    [
        ({"loggedIn": False}, "loggedIn"),
        ({"authMethod": "api_key"}, "authMethod"),
        ({"subscriptionType": "max"}, "subscriptionType"),
        ({"subscriptionType": "free"}, "subscriptionType"),
        ({"apiProvider": "bedrock"}, "apiProvider"),
    ],
)
def test_login_gate_refuses_anything_but_the_required_login(
    override: dict[str, Any], field: str
) -> None:
    status = {**GOOD_STATUS, **override}
    with pytest.raises(cli.ClaudeCLIAuthError) as raised:
        cli.verify_subscription_login(status)
    assert field in str(raised.value)


def test_from_verified_subscription_uses_fake_auth_status_without_leaking_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.dumps(GOOD_STATUS)
    fake = _fake_executable(
        tmp_path / "claude",
        f"import sys\nassert sys.argv[1:] == ['auth', 'status']\nprint({payload!r})\n",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    provider = cli.ClaudeCLIProvider.from_verified_subscription(binary=str(fake), model="m")
    assert provider.usage_summary()["verified_login"]["api_key_env_present_and_stripped"] is True
    assert "person@example.invalid" not in json.dumps(provider.usage_summary())


def test_from_verified_subscription_refuses_a_mismatched_login(tmp_path: Path) -> None:
    payload = json.dumps({**GOOD_STATUS, "subscriptionType": "free"})
    fake = _fake_executable(tmp_path / "claude", f"print({payload!r})\n")
    with pytest.raises(cli.ClaudeCLIAuthError, match="subscriptionType"):
        cli.ClaudeCLIProvider.from_verified_subscription(binary=str(fake), model="m")


def test_missing_binary_is_reported_as_not_found(tmp_path: Path) -> None:
    with pytest.raises(cli.ClaudeCLINotFoundError):
        cli.read_auth_status(str(tmp_path / "absent"), {})


# ---------------------------------------------------------------- end-to-end with a fake CLI


def _run_create(provider: cli.ClaudeCLIProvider, **kwargs: Any) -> cli.Response:
    messages = [{"role": "user", "content": "Fix it."}]
    return asyncio.run(
        provider.messages.create(system="SYS", messages=messages, tools=TOOLS, **kwargs)
    )


def test_create_maps_fake_cli_reply_to_tool_blocks_with_unmetered_cost(tmp_path: Path) -> None:
    record = tmp_path / "record.json"
    fake = _fake_executable(
        tmp_path / "claude",
        "import json, os, sys\n"
        "stdin = sys.stdin.read()\n"
        "argv = sys.argv[1:]\n"
        "assert argv[argv.index('--tools') + 1] == ''\n"
        "assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        "assert 'SYS' in stdin and 'propose_patch' in stdin\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'cwd': os.getcwd(), 'argv': argv}}))\n"
        "print(json.dumps({'is_error': False, 'result': '', 'num_turns': 1,"
        " 'structured_output': {'text': 'ok', 'tool_calls': ["
        "{'name': 'run_tests', 'arguments_json': '{\"test_path\": \"t.py\"}'}]},"
        " 'usage': {'input_tokens': 4, 'output_tokens': 2, 'cache_read_input_tokens': 6},"
        " 'total_cost_usd': 1.5}))\n",
    )
    provider = cli.ClaudeCLIProvider(binary=str(fake), model="claude-sonnet-5-5", verified_login={})
    os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-used"
    try:
        response = _run_create(provider)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    assert response.stop_reason == "tool_use"
    assert response.usage.input_tokens == 4 + 6  # cache reads are counted toward the token cap
    assert response.usage.output_tokens == 2
    assert response.usage.cost_usd is None
    tool_block = response.content[-1]
    assert (tool_block.type, tool_block.name, tool_block.input) == (
        "tool_use",
        "run_tests",
        {"test_path": "t.py"},
    )

    seen = json.loads(record.read_text(encoding="utf-8"))
    assert not Path(seen["cwd"]).resolve().is_relative_to(REPO_ROOT)

    summary = provider.usage_summary()
    assert summary["metered"] is False
    assert summary["billed_cost_usd"] is None
    assert summary["cost_enforcement"] == "not_enforced_subscription_unmetered"
    assert summary["unverified_api_equivalent_estimate_usd"] == "1.5"
    assert summary["cli_invocations"] == 1


def test_create_final_reply_has_end_turn_and_no_tool_blocks(tmp_path: Path) -> None:
    fake = _fake_executable(
        tmp_path / "claude",
        "import json\n"
        "print(json.dumps({'is_error': False, 'result': '', 'structured_output':"
        " {'text': 'summary', 'tool_calls': []}, 'usage': {}}))\n",
    )
    provider = cli.ClaudeCLIProvider(binary=str(fake), model="m", verified_login={})
    response = _run_create(provider)
    assert response.stop_reason == "end_turn"
    assert [block.type for block in response.content] == ["text"]


def test_create_times_out_and_kills_the_child(tmp_path: Path) -> None:
    fake = _fake_executable(tmp_path / "claude", "import time\ntime.sleep(30)\n")
    provider = cli.ClaudeCLIProvider(binary=str(fake), model="m", verified_login={})
    with pytest.raises(cli.ClaudeCLITimeoutError):
        _run_create(provider, request_timeout_seconds=0.5)


def test_create_refuses_a_request_without_tools(tmp_path: Path) -> None:
    provider = cli.ClaudeCLIProvider(binary="/nonexistent", model="m", verified_login={})
    with pytest.raises(cli.ClaudeCLIProviderError):
        asyncio.run(provider.messages.create(system="", messages=[], tools=[]))


# ---------------------------------------------------------------- Gemini path is unchanged


def test_gemini_remains_the_default_provider_in_the_evaluator() -> None:
    default = inspect.signature(ue._execute).parameters["provider_name"].default
    assert default == "gemini_api"


def test_gemini_frozen_configs_still_validate_with_their_pinned_hashes() -> None:
    budget = json.loads(ue.BUDGET_CONFIG_PATH.read_text(encoding="utf-8"))
    ue.validate_budget_config(budget)
    assert ue.sha256_file(ue.BUDGET_CONFIG_PATH) == ue.EXPECTED_BUDGET_CONFIG_SHA256
    context = json.loads(ue.CONTEXT_POLICY_PATH.read_text(encoding="utf-8"))
    ue.validate_context_policy(context)
    assert ue.sha256_file(ue.CONTEXT_POLICY_PATH) == ue.EXPECTED_CONTEXT_POLICY_SHA256
