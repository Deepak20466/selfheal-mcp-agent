from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any

from healer.mcp_client import MCPToolError
from healer.runtime_agent import _run_one_attempt


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=arguments)


def _response(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        usage=SimpleNamespace(
            input_tokens=8,
            output_tokens=3,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class _Provider:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses
        self.calls = 0
        self.request_messages: list[list[dict[str, Any]]] = []
        self.messages = self

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        self.request_messages.append(copy.deepcopy(kwargs["messages"]))
        return self.responses.pop(0)


class _MCP:
    def __init__(self) -> None:
        self.patch_calls = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "propose_patch":
            self.patch_calls += 1
            if self.patch_calls == 1:
                raise MCPToolError("git apply rejected malformed unified diff")
            return {"applied": True, "diff_stat": "1 file changed"}
        if name == "run_tests":
            return {"passed": True, "output": "regression test passed"}
        raise AssertionError(f"unexpected tool call: {name}")


def test_agent_recovers_once_from_failed_patch_and_keeps_test_proof_strict() -> None:
    provider = _Provider(
        [
            _response(_tool_call("p1", "propose_patch", {"unified_diff": "bad diff"})),
            _response(_tool_call("p2", "propose_patch", {"unified_diff": "corrected diff"})),
            _response(_tool_call("t1", "run_tests", {"test_path": "tests/test_fix.py"})),
            _response(SimpleNamespace(type="text", text="Repair ready for independent checks.")),
        ]
    )
    mcp = _MCP()

    result = asyncio.run(
        _run_one_attempt(
            anthropic_client=provider,
            mcp=mcp,  # type: ignore[arg-type]
            job_id=9,
            worktree_name="test-worktree",
            messages=[{"role": "user", "content": "repair this frozen failure"}],
            tool_schemas=[],
            max_total_tokens=1_000,
            model_name="offline-test-model",
        )
    )

    assert provider.calls == 4
    assert mcp.patch_calls == 2
    assert result.success is False  # One passing run does not prove fail-before-pass.
    recovery_history = provider.request_messages[1]
    tool_result = recovery_history[-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "git apply rejected malformed unified diff" in tool_result["content"]


def test_agent_stops_after_repeated_identical_failed_tool_call() -> None:
    provider = _Provider(
        [
            _response(_tool_call("r1", "read_file", {"path": "src/a.py", "start_line": [1, 1]})),
            _response(_tool_call("r2", "read_file", {"path": "src/a.py", "start_line": [1, 1]})),
            _response(SimpleNamespace(type="text", text="This response must not be requested.")),
        ]
    )

    class AlwaysFails:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            raise MCPToolError("start_line must be an integer")

    result = asyncio.run(
        _run_one_attempt(
            anthropic_client=provider,
            mcp=AlwaysFails(),  # type: ignore[arg-type]
            job_id=10,
            worktree_name="test-worktree",
            messages=[{"role": "user", "content": "repair this frozen failure"}],
            tool_schemas=[],
            max_total_tokens=1_000,
            model_name="offline-test-model",
        )
    )

    assert provider.calls == 2
    assert result.success is False
    assert "repeated identical failed tool call" in result.root_cause


def test_agent_normalizes_observed_two_line_read_range_shape() -> None:
    provider = _Provider(
        [
            _response(
                _tool_call(
                    "r1",
                    "read_file",
                    {"path": "src/a.py", "start_line": [1, 200]},
                )
            ),
            _response(SimpleNamespace(type="text", text="Inspection complete.")),
        ]
    )

    class ReadCapture:
        arguments: dict[str, Any] | None = None

        async def call_tool(self, _name: str, arguments: dict[str, Any]) -> Any:
            self.arguments = arguments
            return {"path": "src/a.py", "content": "source"}

    mcp = ReadCapture()
    asyncio.run(
        _run_one_attempt(
            anthropic_client=provider,
            mcp=mcp,  # type: ignore[arg-type]
            job_id=11,
            worktree_name="test-worktree",
            messages=[{"role": "user", "content": "inspect source"}],
            tool_schemas=[],
            max_total_tokens=1_000,
            model_name="offline-test-model",
        )
    )

    assert mcp.arguments == {"path": "src/a.py", "start_line": 1, "end_line": 200}
    assert provider.calls == 2


def test_agent_stops_after_four_inspection_only_turns() -> None:
    provider = _Provider(
        [
            _response(
                _tool_call(
                    f"r{start}",
                    "read_file",
                    {"path": "src/a.py", "start_line": start, "end_line": start + 9},
                )
            )
            for start in (1, 11, 21, 31, 41)
        ]
    )

    class ReadSource:
        async def call_tool(self, _name: str, _arguments: dict[str, Any]) -> Any:
            return {"path": "src/a.py", "content": "source"}

    result = asyncio.run(
        _run_one_attempt(
            anthropic_client=provider,
            mcp=ReadSource(),  # type: ignore[arg-type]
            job_id=12,
            worktree_name="test-worktree",
            messages=[{"role": "user", "content": "inspect source"}],
            tool_schemas=[],
            max_total_tokens=1_000,
            model_name="offline-test-model",
        )
    )

    assert provider.calls == 4
    assert result.success is False
    assert "inspection-only tool-turn limit" in result.root_cause
