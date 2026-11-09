"""healer/api_adapters.py: GroqClient/GeminiApiClient/OpenRouterClient, the
AnthropicClientLike adapters over each provider's real free-tier HTTP API.
All HTTP mocked via respx -- see CLAUDE.md "Anthropic and GitHub are always
mocked in tests", extended here to the three API-key backends.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from healer.api_adapters import (
    GEMINI_API_BASE,
    GROQ_API_BASE,
    OPENROUTER_API_BASE,
    GeminiApiClient,
    GroqClient,
    OpenRouterClient,
)
from healer.backend_chain import BackendCooldownError


def _assert_gemini_parts_equal(actual: Any, expected: Any) -> None:
    if actual != expected:
        pytest.fail("Gemini model Parts changed during round-trip (opaque fields suppressed)")


TOOLS = [
    {
        "name": "get_error",
        "description": "Get an error by id",
        "input_schema": {
            "type": "object",
            "properties": {"error_id": {"type": "integer"}},
            "required": ["error_id"],
        },
    }
]


async def test_groq_text_only_response(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hello there"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    client = GroqClient(api_key="gsk_fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "be helpful"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    assert response.content[0].text == "hello there"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


async def test_groq_retries_gateway_failure_with_jittered_backoff(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(api_adapters.asyncio, "sleep", record_sleep)

    class DeterministicRandom:
        def uniform(self, _low: float, high: float) -> float:
            return high

    monkeypatch.setattr(api_adapters.random, "SystemRandom", DeterministicRandom)
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(503, text="gateway unavailable"),
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "recovered"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            ),
        ]
    )

    response = await GroqClient(api_key="gsk_fake").messages.create(
        model=None,
        max_tokens=10,
        system=[],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )

    assert route.call_count == 2
    assert delays == [0.25]
    assert response.content[0].text == "recovered"


async def test_groq_tool_call_response_and_round_trip(respx_mock: Any) -> None:
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_error",
                                        "arguments": '{"error_id": 7}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                },
            },
        )
    )
    client = GroqClient(api_key="gsk_fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=[{"role": "user", "content": "get error 7"}],
    )
    block = response.content[0]
    assert block.type == "tool_use"
    assert block.name == "get_error"
    assert block.input == {"error_id": 7}

    # Now round-trip: append the assistant turn + a tool_result, as
    # runtime_agent.py's real loop does, and confirm the translator produces
    # a valid OpenAI-shaped tool message.
    messages = [
        {"role": "user", "content": "get error 7"},
        {"role": "assistant", "content": response.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": '{"exception_type": "KeyError"}',
                    "is_error": False,
                }
            ],
        },
    ]
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "It's a KeyError."}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2},
            },
        )
    )
    second = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=messages,
    )
    assert second.content[0].text == "It's a KeyError."
    sent_body = route.calls.last.request.content
    assert b"tool_call_id" in sent_body
    assert b"KeyError" in sent_body


async def test_groq_preserves_parallel_and_sequential_tool_history(respx_mock: Any) -> None:
    groq_tools = [
        {
            "name": "read_file",
            "description": "Read a repository file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "run_tests",
            "description": "Run a test path",
            "input_schema": {
                "type": "object",
                "properties": {"test_path": {"type": "string"}},
            },
        },
    ]
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_read",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"src/app.py"}',
                                        },
                                    },
                                    {
                                        "id": "call_tests",
                                        "type": "function",
                                        "function": {
                                            "name": "run_tests",
                                            "arguments": '{"test_path":"tests/test_app.py"}',
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_tests_again",
                                        "type": "function",
                                        "function": {
                                            "name": "run_tests",
                                            "arguments": '{"test_path":"tests/test_app.py"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Done."}}]},
            ),
        ]
    )
    client = GroqClient(api_key="offline-test-key")
    messages: list[dict[str, Any]] = [{"role": "user", "content": "Fix and verify."}]

    first = await client.messages.create(
        model=None, max_tokens=100, system=[], tools=groq_tools, messages=messages
    )
    messages.extend(
        [
            {"role": "assistant", "content": first.content},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_read", "content": "source"},
                    {"type": "tool_result", "tool_use_id": "call_tests", "content": "failed"},
                ],
            },
        ]
    )
    second = await client.messages.create(
        model=None, max_tokens=100, system=[], tools=groq_tools, messages=messages
    )
    messages.extend(
        [
            {"role": "assistant", "content": second.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_tests_again",
                        "content": "passed",
                    }
                ],
            },
        ]
    )
    final = await client.messages.create(
        model=None, max_tokens=100, system=[], tools=groq_tools, messages=messages
    )

    assert final.content[0].text == "Done."
    requests = [json.loads(call.request.content) for call in route.calls]
    assert [call["id"] for call in requests[1]["messages"][2]["tool_calls"]] == [
        "call_read",
        "call_tests",
    ]
    assert [
        (message["tool_call_id"], message["content"])
        for message in requests[1]["messages"]
        if message["role"] == "tool"
    ] == [("call_read", "source"), ("call_tests", "failed")]
    assert requests[2]["messages"][-2]["tool_calls"][0]["id"] == "call_tests_again"
    assert requests[2]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_tests_again",
        "content": "passed",
    }


async def test_groq_uses_propagated_case_deadline_for_http_timeout(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters

    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "bounded"}}]}
        )
    )
    constructed_timeouts: list[httpx.Timeout | float] = []
    real_async_client = httpx.AsyncClient

    def capture_timeout(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        constructed_timeouts.append(kwargs["timeout"])
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(api_adapters.httpx, "AsyncClient", capture_timeout)
    response = await GroqClient(api_key="offline-test-key").messages.create(
        model=None,
        max_tokens=10,
        system=[],
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
        request_timeout_seconds=17.5,
    )

    assert response.content[0].text == "bounded"
    assert isinstance(constructed_timeouts[0], httpx.Timeout)
    for timeout in (
        constructed_timeouts[0].connect,
        constructed_timeouts[0].read,
        constructed_timeouts[0].write,
        constructed_timeouts[0].pool,
    ):
        assert timeout <= 17.5
    assert timeout == pytest.approx(17.5, abs=0.05)


async def test_groq_read_timeout_is_sanitized_and_not_retried(respx_mock: Any) -> None:
    from healer.api_adapters import GroqAPIRequestTimeout

    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=httpx.ReadTimeout("offline timeout fixture")
    )

    with pytest.raises(GroqAPIRequestTimeout, match="Groq chat/completions HTTP ReadTimeout"):
        await GroqClient(api_key="offline-test-key").messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hello"}],
        )

    assert route.call_count == 1


async def test_groq_caps_output_reservation_without_truncating_tool_history(
    respx_mock: Any,
) -> None:
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "continue"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    )
    client = GroqClient(api_key="offline-test-key")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Repair the failure."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_read",
                    "name": "read_file",
                    "input": {"path": "src/app.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_read", "content": "source context"}
            ],
        },
    ]
    expected_history = client.messages._to_openai_messages([], messages)

    await client.messages.create(
        model=None,
        max_tokens=7843,
        system=[],
        tools=TOOLS,
        messages=messages,
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == 4096
    assert 1366 + sent["max_tokens"] < 8000
    assert sent["messages"] == expected_history
    assert sent["messages"][2]["tool_calls"][0]["id"] == "call_read"
    assert sent["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_read",
        "content": "source context",
    }


async def test_groq_retries_recorded_tpm_limit_once_after_server_delay(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(api_adapters.asyncio, "sleep", record_sleep)
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                429,
                json={
                    "error": {
                        "message": (
                            "Rate limit reached on tokens per minute (TPM): Limit 8000, "
                            "Used 1366, Requested 7843. Please try again in 9.0675s."
                        ),
                        "type": "tokens",
                        "code": "rate_limit_exceeded",
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "recovered"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ),
        ]
    )
    messages = [
        {"role": "user", "content": "Repair the failure."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_tests",
                    "name": "run_tests",
                    "input": {"test_path": "tests/test_app.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_tests", "content": "failed"}],
        },
    ]

    response = await GroqClient(api_key="offline-test-key").messages.create(
        model=None, max_tokens=7843, system=[], tools=TOOLS, messages=messages
    )

    assert response.content[0].text == "recovered"
    assert delays == [9.0675]
    assert route.call_count == 2
    requests = [json.loads(call.request.content) for call in route.calls]
    assert [request["max_tokens"] for request in requests] == [4096, 4096]
    assert requests[0]["messages"] == requests[1]["messages"]
    assert requests[0]["messages"][2]["tool_calls"][0]["id"] == "call_tests"


async def test_groq_v2_tpm_failure_honors_5630_7689_and_398925_second_delay(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(api_adapters.asyncio, "sleep", record_sleep)
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                429,
                json={
                    "error": {
                        "message": (
                            "Rate limit reached on tokens per minute (TPM): Limit 8000, "
                            "Used 5630, Requested 7689. Please try again in 39.8925s."
                        ),
                        "type": "tokens",
                        "code": "rate_limit_exceeded",
                    }
                },
            ),
            httpx.Response(
                200,
                headers={"x-ratelimit-remaining-tokens": "7998"},
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "continue"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ),
        ]
    )
    messages = [
        {"role": "user", "content": "Repair the failure."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_run_tests_v2",
                    "name": "run_tests",
                    "input": {"test_path": "tests/test_app.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_run_tests_v2",
                    "content": "failed test evidence",
                }
            ],
        },
    ]

    response = await GroqClient(api_key="offline-test-key").messages.create(
        model=None,
        max_tokens=7843,
        system=[],
        tools=TOOLS,
        messages=messages,
        request_timeout_seconds=240,
    )

    assert response.content[0].text == "continue"
    assert delays == [39.8925]
    assert route.call_count == 2
    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] <= 4096
    assert sent["messages"][2]["tool_calls"][0]["id"] == "call_run_tests_v2"


async def test_groq_v2_context_growth_compacts_list_files_without_breaking_tool_order(
    respx_mock: Any,
) -> None:
    from healer.api_adapters import _groq_prompt_token_estimate

    list_tools = [
        {
            "name": "list_files",
            "description": "List repository paths matching a glob",
            "input_schema": {"type": "object", "properties": {"glob": {"type": "string"}}},
        }
    ]
    files = [f"package/module_{index:05d}/source_{index:05d}.py" for index in range(2000)]
    first_call = {
        "id": "call_files_v2",
        "type": "function",
        "function": {"name": "list_files", "arguments": "{}"},
    }
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={
                    "x-ratelimit-remaining-tokens": "5000",
                    "x-ratelimit-reset-tokens": "20s",
                },
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [first_call],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1503, "completion_tokens": 35},
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "I will inspect a file."},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 8},
                },
            ),
        ]
    )
    client = GroqClient(api_key="offline-test-key")
    initial = [{"role": "user", "content": "Find the failing module."}]
    response = await client.messages.create(
        model=None, max_tokens=4096, system=[], tools=list_tools, messages=initial
    )
    history = initial + [
        {"role": "assistant", "content": response.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_files_v2",
                    "content": json.dumps(files),
                }
            ],
        },
    ]
    second = await client.messages.create(
        model=None, max_tokens=4096, system=[], tools=list_tools, messages=history
    )

    assert second.content[0].text == "I will inspect a file."
    assert route.call_count == 2
    _, second_payload = [json.loads(call.request.content) for call in route.calls]
    assistant_tool_calls = next(
        message["tool_calls"] for message in second_payload["messages"] if message.get("tool_calls")
    )
    assert assistant_tool_calls[0]["id"] == "call_files_v2"
    tool_result = second_payload["messages"][-1]["content"]
    assert "Selfheal omitted" in tool_result
    assert "call list_files with a narrower glob" in tool_result
    assert "module_00000/source_00000.py" in tool_result
    assert "module_01999/source_01999.py" in tool_result
    assert [m.get("tool_call_id") for m in second_payload["messages"] if m["role"] == "tool"] == [
        "call_files_v2"
    ]
    assert _groq_prompt_token_estimate(second_payload["messages"], second_payload["tools"]) <= 5000
    assert second_payload["max_tokens"] <= 4096
    assert (
        second_payload["max_tokens"]
        + _groq_prompt_token_estimate(second_payload["messages"], second_payload["tools"])
        <= 5000
    )


async def test_groq_compacts_large_read_file_context_and_keeps_line_ranges_retrievable(
    respx_mock: Any,
) -> None:
    from healer.api_adapters import _groq_prompt_token_estimate

    read_tools = [
        {
            "name": "read_file",
            "description": "Read a file or a 1-indexed line range",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        }
    ]
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"x-ratelimit-remaining-tokens": "4889"},
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_read_source",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "app/routers/fees.py",
                                                    "start_line": 1,
                                                    "end_line": 1000,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 2718, "completion_tokens": 56},
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "continue"}}],
                    "usage": {"prompt_tokens": 2000, "completion_tokens": 100},
                },
            ),
        ]
    )
    client = GroqClient(api_key="offline-test-key")
    initial = [{"role": "user", "content": "Inspect the failing source."}]
    first = await client.messages.create(
        model=None, max_tokens=4096, system=[], tools=read_tools, messages=initial
    )
    original_file_result = {
        "path": "app/routers/fees.py",
        "start_line": 1,
        "end_line": 1000,
        "total_lines": 1000,
        "content": "\n".join(f"source line {line:04d} = {line}" for line in range(1, 1001)),
    }
    history = initial + [
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_read_source",
                    "content": json.dumps(original_file_result),
                }
            ],
        },
    ]
    uncompact_messages = client.messages._to_openai_messages([], history)
    uncompact_tools = client.messages._to_openai_tools(read_tools)
    assert _groq_prompt_token_estimate(uncompact_messages, uncompact_tools) > 8000

    second = await client.messages.create(
        model=None, max_tokens=4096, system=[], tools=read_tools, messages=history
    )

    assert second.content[0].text == "continue"
    assert route.call_count == 2
    first_payload, second_payload = [json.loads(call.request.content) for call in route.calls]
    compact_result = second_payload["messages"][-1]["content"]
    assert "Selfheal omitted source lines" in compact_result
    assert "retrieve them with read_file start_line/end_line" in compact_result
    assert "source line 0001 = 1" in compact_result
    assert "source line 1000 = 1000" in compact_result
    assert (
        _groq_prompt_token_estimate(second_payload["messages"], second_payload["tools"])
        + second_payload["max_tokens"]
        <= 4889
    )
    assert len(history[-1]["content"][0]["content"]) > len(compact_result)
    assert first_payload["max_tokens"] <= 4096


async def test_groq_v3_budget_waits_for_reset_when_history_fits_fresh_tpm(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    original_estimate = api_adapters._groq_prompt_token_estimate

    def estimate_history(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        if any(message.get("role") == "tool" for message in messages):
            return 6818
        return original_estimate(messages, tools)

    monkeypatch.setattr(api_adapters.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(api_adapters, "_groq_prompt_token_estimate", estimate_history)
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={
                    "x-ratelimit-remaining-tokens": "5936",
                    "x-ratelimit-reset-tokens": "5s",
                },
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_read_context",
                                        "type": "function",
                                        "function": {
                                            "name": "get_error",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1505, "completion_tokens": 89},
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "continue repair"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ),
        ]
    )
    client = GroqClient(api_key="offline-test-key")
    initial = [
        {"role": "user", "content": "Repair the failing test without changing its contract."}
    ]
    first = await client.messages.create(
        model=None, max_tokens=4096, system=[], tools=TOOLS, messages=initial
    )
    history = initial + [
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_read_context",
                    "content": "failing test and relevant source context",
                }
            ],
        },
    ]

    second = await client.messages.create(
        model=None,
        max_tokens=4096,
        system=[],
        tools=TOOLS,
        messages=history,
        request_timeout_seconds=120,
    )

    assert second.content[0].text == "continue repair"
    # The reset deadline is set when the first response arrives, so local
    # preflight time is subtracted before sleeping. Keep a narrow tolerance
    # while still proving we honor the server's approximately five-second
    # reset rather than retrying immediately or waiting longer.
    assert len(delays) == 1
    assert 4.9 <= delays[0] <= 5.0
    assert route.call_count == 2
    resumed_request = json.loads(route.calls[1].request.content)
    assert resumed_request["max_tokens"] == 1182
    assert resumed_request["max_tokens"] + 6818 == 8000
    assistant_message = next(
        message for message in resumed_request["messages"] if message.get("tool_calls")
    )
    tool_message = next(
        message for message in resumed_request["messages"] if message["role"] == "tool"
    )
    assert assistant_message["tool_calls"][0]["id"] == "call_read_context"
    assert tool_message["tool_call_id"] == "call_read_context"
    assert tool_message["content"] == "failing test and relevant source context"


async def test_groq_v3_budget_rejects_history_that_cannot_fit_fresh_tpm(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters
    from healer.api_adapters import GroqRequestBudgetError

    original_estimate = api_adapters._groq_prompt_token_estimate

    def estimate_history(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        if any(message.get("role") == "tool" for message in messages):
            return 7800
        return original_estimate(messages, tools)

    monkeypatch.setattr(api_adapters, "_groq_prompt_token_estimate", estimate_history)
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            headers={
                "x-ratelimit-remaining-tokens": "5936",
                "x-ratelimit-reset-tokens": "5s",
            },
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_context",
                                    "type": "function",
                                    "function": {"name": "get_error", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1505, "completion_tokens": 89},
            },
        )
    )
    client = GroqClient(api_key="offline-test-key")
    initial = [{"role": "user", "content": "Repair the failing test."}]
    first = await client.messages.create(
        model=None, max_tokens=4096, system=[], tools=TOOLS, messages=initial
    )
    history = initial + [
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_context",
                    "content": "source and test evidence",
                }
            ],
        },
    ]

    with pytest.raises(GroqRequestBudgetError, match="cannot fit a fresh 8000-token TPM window"):
        await client.messages.create(
            model=None,
            max_tokens=4096,
            system=[],
            tools=TOOLS,
            messages=history,
            request_timeout_seconds=120,
        )

    assert route.call_count == 1


async def test_groq_request_too_large_fails_closed_without_dropping_read_file_evidence(
    respx_mock: Any,
) -> None:
    from healer.api_adapters import GroqRequestBudgetError

    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [], "usage": {}})
    )
    messages = [
        {"role": "user", "content": "Repair this exact source and failing test."},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_read_large",
                    "name": "read_file",
                    "input": {"path": "src/important.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_read_large",
                    "content": "source evidence " + ("x" * 40000),
                }
            ],
        },
    ]

    with pytest.raises(GroqRequestBudgetError, match="request not sent.*cannot fit"):
        await GroqClient(api_key="offline-test-key").messages.create(
            model=None, max_tokens=4096, system=[], tools=TOOLS, messages=messages
        )

    assert route.call_count == 0
    assert "call_read_large" == messages[1]["content"][0]["id"]
    assert len(messages[2]["content"][0]["content"]) > 40000


def test_groq_test_output_compression_keeps_failure_names_errors_and_tracebacks() -> None:
    from healer.api_adapters import _groq_compact_test_output

    output = "\n".join(
        ["routine progress line" for _ in range(80)]
        + [
            "FAILED tests/test_repair.py::test_handles_empty_config - AssertionError",
            "Traceback (most recent call last):",
            '  File "tests/test_repair.py", line 42, in test_handles_empty_config',
            "    assert actual == expected",
            "E   AssertionError: expected populated configuration",
            "short test summary info",
        ]
    )
    result = _groq_compact_test_output(
        json.dumps({"passed": False, "output": output}), max_bytes=900
    )

    assert result is not None
    compacted = json.loads(result)
    assert compacted["passed"] is False
    assert "test_handles_empty_config" in compacted["output"]
    assert "AssertionError: expected populated configuration" in compacted["output"]
    assert 'File "tests/test_repair.py", line 42' in compacted["output"]
    assert "complete output remains in local tool history" in compacted["output"]
    assert len(result.encode("utf-8")) <= 900


async def test_groq_tpm_retry_is_skipped_when_server_delay_exceeds_case_deadline(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters

    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(api_adapters.asyncio, "sleep", record_sleep)
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "message": (
                        "Rate limit reached on tokens per minute (TPM): Limit 8000, "
                        "Used 5630, Requested 7689. Please try again in 39.8925s."
                    )
                }
            },
        )
    )

    with pytest.raises(BackendCooldownError):
        await GroqClient(api_key="offline-test-key").messages.create(
            model=None,
            max_tokens=7843,
            system=[],
            tools=TOOLS,
            messages=[{"role": "user", "content": "Repair the failure."}],
            request_timeout_seconds=10,
        )

    assert route.call_count == 1
    assert delays == []


async def test_groq_length_400_is_reported_with_local_budget_diagnostics(respx_mock: Any) -> None:
    from healer.api_adapters import GroqRequestBudgetError

    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "Please reduce the length of the messages or completion."}},
        )
    )

    with pytest.raises(GroqRequestBudgetError, match="HTTP 400.*prompt_estimate=.*context_limit="):
        await GroqClient(api_key="offline-test-key").messages.create(
            model=None,
            max_tokens=100,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "small test request"}],
        )


async def test_groq_daily_quota_is_not_retried(respx_mock: Any) -> None:
    from healer.api_adapters import GroqDailyQuotaError

    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            429,
            headers={"retry-after": "86400"},
            json={
                "error": {
                    "message": "Rate limit reached on tokens per day (TPD): daily quota exhausted",
                    "type": "tokens",
                    "code": "rate_limit_exceeded",
                }
            },
        )
    )

    with pytest.raises(GroqDailyQuotaError):
        await GroqClient(api_key="offline-test-key").messages.create(
            model=None,
            max_tokens=100,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hello"}],
        )

    assert route.call_count == 1


async def test_groq_output_cap_reports_incomplete_response(respx_mock: Any) -> None:
    from healer.api_adapters import GroqOutputTokenLimit

    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "partial"},
                        "finish_reason": "length",
                    }
                ]
            },
        )
    )

    with pytest.raises(GroqOutputTokenLimit, match="reached the output-token cap"):
        await GroqClient(api_key="offline-test-key").messages.create(
            model=None,
            max_tokens=7843,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hello"}],
        )


async def test_groq_429_raises_cooldown_with_retry_after(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(429, headers={"retry-after": "120"}, text="rate limited")
    )
    client = GroqClient(api_key="gsk_fake")
    with pytest.raises(BackendCooldownError) as exc_info:
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_info.value.retry_after_seconds == 120


async def test_groq_401_raises_cooldown(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(401, text="invalid api key")
    )
    client = GroqClient(api_key="gsk_fake")
    with pytest.raises(BackendCooldownError):
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )


async def test_groq_500_raises_plain_runtime_error(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(500, text="server error")
    )
    client = GroqClient(api_key="gsk_fake")
    with pytest.raises(RuntimeError):
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )


def test_groq_client_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "groq_api_key", None)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqClient(api_key=None)


async def test_gemini_text_only_response(respx_mock: Any) -> None:
    route = respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi from gemini"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
            },
        )
    )
    client = GeminiApiClient(api_key="fake-key")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "be helpful"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert response.content[0].type == "text"
    assert response.content[0].text == "hi from gemini"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 4
    assert route.calls.last.request.headers["x-goog-api-key"] == "fake-key"
    assert "key" not in route.calls.last.request.url.params


async def test_gemini_uses_propagated_case_deadline_for_http_timeout(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import healer.api_adapters as api_adapters

    respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "bounded"}]}}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )
    )
    constructed_timeouts: list[httpx.Timeout | float] = []
    real_async_client = httpx.AsyncClient

    def capture_timeout(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        constructed_timeouts.append(kwargs["timeout"])
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(api_adapters.httpx, "AsyncClient", capture_timeout)

    response = await GeminiApiClient(api_key="fake-key").messages.create(
        model=None,
        max_tokens=10,
        system=[],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
        request_timeout_seconds=17.5,
    )

    assert response.content[0].text == "bounded"
    assert len(constructed_timeouts) == 1
    assert isinstance(constructed_timeouts[0], httpx.Timeout)
    assert constructed_timeouts[0].connect == 17.5
    assert constructed_timeouts[0].read == 17.5
    assert constructed_timeouts[0].write == 17.5
    assert constructed_timeouts[0].pool == 17.5


async def test_gemini_read_timeout_names_provider_request_without_retry(respx_mock: Any) -> None:
    from healer.api_adapters import GeminiAPIRequestTimeout

    route = respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        side_effect=httpx.ReadTimeout("read timeout")
    )

    with pytest.raises(GeminiAPIRequestTimeout, match="Gemini generateContent HTTP ReadTimeout"):
        await GeminiApiClient(api_key="fake-key").messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )

    assert route.call_count == 1


async def test_gemini_function_call_response(respx_mock: Any) -> None:
    respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "get_error", "args": {"error_id": 9}}}
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )
    )
    client = GeminiApiClient(api_key="fake-key")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=[{"role": "user", "content": "get error 9"}],
    )
    block = response.content[0]
    assert block.type == "tool_use"
    assert block.name == "get_error"
    assert block.input == {"error_id": 9}


async def test_gemini_preserves_function_parts_signatures_and_response_ids(
    respx_mock: Any,
) -> None:
    """Model Parts must round-trip unchanged; fixture signatures are opaque mock data only."""
    fixture_signature_1 = "offline-fixture-signature-one-not-valid-for-provider-use"
    fixture_signature_2 = "offline-fixture-signature-two-not-valid-for-provider-use"
    fixture_signature_3 = "offline-fixture-signature-three-not-valid-for-provider-use"
    first_model_parts = [
        {
            "functionCall": {
                "id": "fixture-call-run-tests",
                "name": "run_tests",
                "args": {"test_path": "tests/test_example.py"},
            },
            "thoughtSignature": fixture_signature_1,
        }
    ]
    second_model_parts = [
        {
            "text": "I will inspect the implementation first.",
            "thoughtSignature": fixture_signature_2,
        },
        {
            "functionCall": {
                "id": "fixture-call-read-file",
                "name": "read_file",
                "args": {"path": "src/example.py"},
            },
            "thoughtSignature": fixture_signature_3,
        },
        {
            "functionCall": {
                "id": "fixture-call-run-tests-again",
                "name": "run_tests",
                "args": {"test_path": "tests/test_example.py"},
            },
            "thoughtSignature": fixture_signature_1,
        },
    ]

    async def generate(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        contents = payload["contents"]
        if len(contents) == 1:
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": first_model_parts}}]},
            )
        if len(contents) == 3:
            # This is the exact v8 failure point: return the reported 400 if
            # the original run_tests Part was reconstructed without its signature.
            if contents[1]["parts"] != first_model_parts:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "message": (
                                "Function call is missing a thought_signature in "
                                "functionCall parts."
                            ),
                        }
                    },
                )
            assert contents[2]["parts"] == [
                {
                    "functionResponse": {
                        "name": "run_tests",
                        "id": "fixture-call-run-tests",
                        "response": {"content": "target test failed as expected"},
                    }
                }
            ]
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": second_model_parts}}]},
            )

        assert len(contents) == 5
        _assert_gemini_parts_equal(contents[1]["parts"], first_model_parts)
        assert contents[2]["parts"][0]["functionResponse"]["id"] == "fixture-call-run-tests"
        _assert_gemini_parts_equal(contents[3]["parts"], second_model_parts)
        # Simulate tool results arriving out of order. They must still match
        # the original function calls and retain the model's part order.
        assert contents[4]["parts"] == [
            {
                "functionResponse": {
                    "name": "read_file",
                    "id": "fixture-call-read-file",
                    "response": {"content": "source contents"},
                }
            },
            {
                "functionResponse": {
                    "name": "run_tests",
                    "id": "fixture-call-run-tests-again",
                    "response": {"content": "target tests passed"},
                }
            },
        ]
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
        )

    route = respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        side_effect=generate
    )
    client = GeminiApiClient(api_key="fake-key")
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Investigate and repair the failure."}
    ]

    first = await client.messages.create(
        model=None, max_tokens=100, system=[], tools=TOOLS, messages=messages
    )
    if fixture_signature_1 in repr(first.content[0]):
        pytest.fail("Gemini thought signature leaked through block representation")
    messages.extend(
        [
            {"role": "assistant", "content": first.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": first.content[0].id,
                        "content": "target test failed as expected",
                    }
                ],
            },
        ]
    )
    second = await client.messages.create(
        model=None, max_tokens=100, system=[], tools=TOOLS, messages=messages
    )
    messages.extend(
        [
            {"role": "assistant", "content": second.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": second.content[2].id,
                        "content": "target tests passed",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": second.content[1].id,
                        "content": "source contents",
                    },
                ],
            },
        ]
    )
    final = await client.messages.create(
        model=None, max_tokens=100, system=[], tools=TOOLS, messages=messages
    )

    assert final.content[0].text == "done"
    assert route.call_count == 3


async def test_gemini_429_raises_cooldown(respx_mock: Any) -> None:
    respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"}, text="quota exceeded")
    )
    client = GeminiApiClient(api_key="fake-key")
    with pytest.raises(BackendCooldownError) as exc_info:
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_info.value.retry_after_seconds == 30


def test_gemini_client_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "gemini_api_key", None)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiApiClient(api_key=None)


async def test_openrouter_text_only_response(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_model", "cohere/north-mini-code:free")
    respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "cohere/north-mini-code:free",
                "choices": [{"message": {"role": "assistant", "content": "hello there"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cost": 0,
                    "is_byok": False,
                },
            },
        )
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "be helpful"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    assert response.content[0].text == "hello there"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


async def test_openrouter_tool_call_response(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_model", "cohere/north-mini-code:free")
    route = respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "cohere/north-mini-code:free",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_error",
                                        "arguments": '{"error_id": 7}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "cost": 0,
                    "is_byok": False,
                },
            },
        )
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=[{"role": "user", "content": "get error 7"}],
    )
    block = response.content[0]
    assert block.type == "tool_use"
    assert block.name == "get_error"
    assert block.input == {"error_id": 7}
    assert route.called
    payload = route.calls[0].request.content
    assert payload is not None
    request_json = json.loads(payload)
    assert request_json["provider"] == {"allow_fallbacks": False}
    assert request_json["usage"] == {"include": True}


async def test_openrouter_compacts_outgoing_read_history_without_mutating_agent_history(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.config import settings as core_settings
    from healer.api_adapters import OPENROUTER_TOOL_RESULT_MAX_BYTES

    monkeypatch.setattr(core_settings, "anthropic_model", "cohere/north-mini-code:free")
    route = respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "cohere/north-mini-code:free",
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0, "is_byok": False},
            },
        )
    )
    content = "\n".join(f"source line {number}" for number in range(1, 1_500))
    tool_result = {
        "path": "app/routers/fees.py",
        "start_line": 1,
        "end_line": 1499,
        "content": content,
    }
    messages = [
        {"role": "user", "content": "inspect the source"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "read-1",
                    "name": "read_file",
                    "input": {"path": "app/routers/fees.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "read-1",
                    "content": json.dumps(tool_result),
                }
            ],
        },
    ]
    original_history = json.dumps(messages)
    client = OpenRouterClient(api_key="sk-or-fake")
    await client.messages.create(
        model=None,
        max_tokens=100,
        system=[],
        tools=TOOLS,
        messages=messages,
    )

    assert json.dumps(messages) == original_history
    payload = json.loads(route.calls[0].request.content or b"{}")
    tool_message = next(message for message in payload["messages"] if message["role"] == "tool")
    assert len(tool_message["content"].encode("utf-8")) <= OPENROUTER_TOOL_RESULT_MAX_BYTES
    assert "omitted source lines" in tool_message["content"]


def test_openrouter_compaction_keeps_tool_results_associated_with_their_turn() -> None:
    from healer.api_adapters import (
        OPENROUTER_TOOL_RESULT_MAX_BYTES,
        _compact_openrouter_tool_messages,
    )

    # Some OpenAI-compatible endpoints can reuse a call ID in a later turn.
    # The older result must still be compacted according to the tool that
    # produced it, rather than whichever tool most recently reused that ID.
    error_result = {
        "id": 197,
        "exception_type": "AssertionError",
        "message": "expected balance to be reconciled",
        "file_path": "tests/test_requested_changes.py",
        "line_number": 317,
        "traceback": "\n".join(f"frame {index}: relevant test evidence" for index in range(600)),
    }
    read_result = {
        "path": "app/routers/fees.py",
        "start_line": 1,
        "end_line": 400,
        "total_lines": 400,
        "content": "\n".join(f"source line {index}: repair-critical code" for index in range(400)),
    }
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "reused-call-id",
                    "function": {"name": "get_error", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "reused-call-id",
            "content": json.dumps(error_result),
        },
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "reused-call-id",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "reused-call-id",
            "content": json.dumps(read_result),
        },
    ]

    compacted = _compact_openrouter_tool_messages(messages)
    tool_results = [message for message in compacted if message["role"] == "tool"]

    assert all(
        len(message["content"].encode("utf-8")) <= OPENROUTER_TOOL_RESULT_MAX_BYTES
        for message in tool_results
    )
    compacted_error = json.loads(tool_results[0]["content"])
    assert compacted_error["exception_type"] == "AssertionError"
    assert compacted_error["message"] == error_result["message"]
    assert compacted_error["file_path"] == error_result["file_path"]
    assert "omitted traceback lines" in compacted_error["traceback"]
    compacted_read = json.loads(tool_results[1]["content"])
    assert "omitted source lines" in compacted_read["content"]


@pytest.mark.parametrize(
    ("model", "usage"),
    [
        ("some/paid-model", {"cost": 0, "is_byok": False}),
        ("google/gemma-4-26b-a4b-it:free", {"cost": 0, "is_byok": False}),
        ("cohere/north-mini-code:free", {"cost": 0.01, "is_byok": False}),
        ("cohere/north-mini-code:free", {"is_byok": False}),
        ("cohere/north-mini-code:free", {"cost": 0, "is_byok": True}),
    ],
)
async def test_openrouter_rejects_unverified_or_paid_completions(
    respx_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    usage: dict[str, Any],
) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_model", "cohere/north-mini-code:free")
    respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": "no patch"}}],
                "usage": usage,
            },
        )
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    with pytest.raises(RuntimeError, match="OpenRouter"):
        await client.messages.create(
            model=None,
            max_tokens=32,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )


async def test_openrouter_rejects_unallowlisted_model_before_request(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_model", "openrouter/free")
    route = respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json={})
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    with pytest.raises(RuntimeError, match="allowlisted free model"):
        await client.messages.create(
            model=None,
            max_tokens=32,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert not route.called


async def test_openrouter_429_raises_cooldown_with_retry_after(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_model", "cohere/north-mini-code:free")
    respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(429, headers={"retry-after": "60"}, text="rate limited")
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    with pytest.raises(BackendCooldownError) as exc_info:
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_info.value.retry_after_seconds == 60


async def test_openrouter_500_raises_plain_runtime_error(respx_mock: Any) -> None:
    respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(500, text="server error")
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    with pytest.raises(RuntimeError):
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )


def test_openrouter_client_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_api_key", None)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterClient(api_key=None)
