"""Claude Code CLI provider for the frozen VIMJ evaluator (Claude Pro login, no API key).

Why this exists: a Claude Pro subscription has no API key. This adapter runs the
official, unmodified `claude` binary in print mode with the operator's own claude.ai
login and exposes the `messages.create` shape the benchmark loop already uses.

Safety properties (each is covered by tests/test_claude_cli_provider.py):
- No API key is read, requested, or forwarded. ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
  ANTHROPIC_BASE_URL and ANTHROPIC_MODEL are removed from the child environment, and the
  login must report authMethod "claude.ai" with subscriptionType "pro" before any call.
- Subscription usage is NOT metered here. Responses carry cost_usd=None, so the $0 cost
  cap is an operator attestation, not enforcement. The CLI's total_cost_usd is an
  API-equivalent estimate and is kept only as an unverified note in the call log.
- Built-in Claude Code tools are disabled (--tools ""), no MCP servers are loaded, and
  sessions are not persisted. The child runs in an empty temporary directory outside
  any repository, so the model cannot read or write files itself.
- Tool use is emulated through a JSON schema whose tool names are an enum of the offered
  tools. An unoffered tool name or malformed arguments fails the attempt instead of
  being guessed at. Every file operation still goes through the evaluator's MCP tools.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CLI_TIMEOUT_SECONDS = 600
CLI_AUTH_TIMEOUT_SECONDS = 30
CLI_MAX_TURNS = 3  # Structured output may need one internal turn; confirm in the live smoke test.
TOOL_RESULT_MAX_CHARS = 8_192
MAX_PROMPT_CHARS = 400_000
REQUIRED_AUTH_METHOD = "claude.ai"
REQUIRED_SUBSCRIPTION_TYPE = "pro"
REQUIRED_API_PROVIDER = "firstParty"
STRIPPED_ENV_VARS = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"}
)
CLI_SYSTEM_PROMPT = (
    "You produce one structured JSON reply for the task transcript on stdin. "
    "You have no tools in this session; follow the response rules in the prompt."
)
RESPONSE_RULES = """RESPONSE RULES:
Reply with one JSON object matching the required schema.
- To use a tool, add one entry to tool_calls: name is one of the listed tool names, and
  arguments_json is a JSON object encoded as a string.
- Make only the calls you need for the next step.
- When the task is finished, return an empty tool_calls array and put your final summary in text.
- Do not claim a test passed unless a TOOL RESULT shows it."""

_USAGE_LIMIT_RE = re.compile(
    r"usage limit|rate.?limit exceeded|quota exceeded|resets? at \d|exceeded.*usage",
    re.IGNORECASE,
)
_NOT_LOGGED_IN_RE = re.compile(
    r"not logged in|please run.{0,20}login|/login|no credentials found|"
    r"invalid api key|unauthorized|authentication (?:failed|required)|please authenticate",
    re.IGNORECASE,
)


class ClaudeCLIProviderError(RuntimeError):
    """Base class for Claude CLI provider failures; every failure fails the attempt closed."""


class ClaudeCLINotFoundError(ClaudeCLIProviderError):
    """The configured claude executable could not be launched."""


class ClaudeCLIAuthError(ClaudeCLIProviderError):
    """The CLI is not logged in with the required claude.ai Pro subscription."""


class ClaudeCLIUsageLimitError(ClaudeCLIProviderError):
    """The subscription's usage limit was reached."""


class ClaudeCLITimeoutError(ClaudeCLIProviderError):
    """The CLI did not finish within the allowed time."""


class ClaudeCLIMalformedOutputError(ClaudeCLIProviderError):
    """The CLI output did not match the required structured-output shape."""


@dataclass
class Block:
    """Content block with the attribute names the benchmark loop reads."""

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: Decimal | None = None


@dataclass
class Response:
    content: list[Block]
    usage: Usage
    stop_reason: str = "end_turn"


@dataclass(frozen=True)
class ParsedTurn:
    text: str
    tool_calls: tuple[tuple[str, dict[str, Any]], ...]
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    num_turns: int
    reported_estimate_usd: str | None


def system_text(system: Any) -> str:
    """Flatten a system prompt given as a string or as text blocks."""
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for item in system:
        text = item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def build_tool_call_schema(tool_names: Sequence[str]) -> dict[str, Any]:
    """JSON schema that constrains the model's reply to offered tool names only."""
    names = sorted(set(tool_names))
    if not names:
        raise ValueError("at least one tool name is required")
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "tool_calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": names},
                        "arguments_json": {"type": "string"},
                    },
                    "required": ["name", "arguments_json"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["text", "tool_calls"],
        "additionalProperties": False,
    }


def _truncate(content: str) -> str:
    if len(content) <= TOOL_RESULT_MAX_CHARS:
        return content
    omitted = len(content) - TOOL_RESULT_MAX_CHARS
    return f"{content[:TOOL_RESULT_MAX_CHARS]}\n[truncated {omitted} characters]"


def _field(block: Any, name: str) -> Any:
    if isinstance(block, Mapping):
        return block.get(name)
    return getattr(block, name, None)


def _render_block(block: Any) -> str:
    kind = _field(block, "type")
    if kind == "text":
        return f"[TEXT]\n{_field(block, 'text') or ''}"
    if kind == "tool_use":
        arguments = json.dumps(_field(block, "input") or {}, sort_keys=True, default=str)
        return (
            f"[ASSISTANT TOOL CALL id={_field(block, 'id')} name={_field(block, 'name')}]\n"
            f"arguments: {arguments}"
        )
    if kind == "tool_result":
        content = _field(block, "content")
        if not isinstance(content, str):
            content = json.dumps(content, default=str)
        is_error = str(bool(_field(block, "is_error"))).lower()
        call_id = _field(block, "tool_use_id")
        return f"[TOOL RESULT for id={call_id} is_error={is_error}]\n{_truncate(content)}"
    raise ClaudeCLIProviderError(f"unsupported content block type: {kind!r}")


def render_transcript(
    system: str, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
) -> str:
    """Serialize system text, offered tools, and the conversation into one prompt."""
    specs = [
        {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool.get("input_schema", {}),
        }
        for tool in tools
    ]
    parts = [
        "SYSTEM INSTRUCTIONS:",
        system or "(none)",
        "",
        "AVAILABLE TOOLS (JSON):",
        json.dumps(specs, indent=2, sort_keys=True),
        "",
        RESPONSE_RULES,
        "",
        "CONVERSATION:",
    ]
    for message in messages:
        parts.append(f"[{str(message['role']).upper()}]")
        content = message["content"]
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.extend(_render_block(block) for block in content)
    prompt = "\n".join(parts)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ClaudeCLIProviderError(
            f"transcript is {len(prompt)} characters, above the {MAX_PROMPT_CHARS} limit"
        )
    return prompt


def _int(mapping: Mapping[str, Any], key: str) -> int:
    try:
        return int(mapping.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _classify_failure(text: str) -> ClaudeCLIProviderError | None:
    if _USAGE_LIMIT_RE.search(text):
        return ClaudeCLIUsageLimitError(text[-2000:])
    if _NOT_LOGGED_IN_RE.search(text):
        return ClaudeCLIAuthError(text[-2000:])
    return None


def parse_turn(stdout: str, stderr: str, returncode: int, tool_names: Sequence[str]) -> ParsedTurn:
    """Validate one `claude -p --output-format json` reply against the offered tools."""
    allowed = set(tool_names)
    combined = f"{stdout}\n{stderr}"
    try:
        payload = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        classified = _classify_failure(combined)
        if classified is not None:
            raise classified
        raise ClaudeCLIMalformedOutputError(
            f"claude CLI did not return a JSON object (exit {returncode})"
        )

    result_text = str(payload.get("result") or "")
    if payload.get("is_error") or returncode != 0:
        classified = _classify_failure(f"{result_text}\n{combined}")
        if classified is not None:
            raise classified
        raise ClaudeCLIProviderError(
            f"claude CLI reported an error (exit {returncode}): {result_text[-500:]}"
        )

    structured = payload.get("structured_output")
    if structured is None and result_text.strip():
        try:
            structured = json.loads(result_text)
        except json.JSONDecodeError:
            structured = None
    if not isinstance(structured, dict):
        raise ClaudeCLIMalformedOutputError("no structured_output object in CLI response")

    text = structured.get("text", "")
    raw_calls = structured.get("tool_calls")
    if not isinstance(text, str) or not isinstance(raw_calls, list):
        raise ClaudeCLIMalformedOutputError("structured_output has the wrong field types")

    calls: list[tuple[str, dict[str, Any]]] = []
    for item in raw_calls:
        if not isinstance(item, dict):
            raise ClaudeCLIMalformedOutputError("tool call entry is not an object")
        name = item.get("name")
        if name not in allowed:
            raise ClaudeCLIMalformedOutputError(f"model requested an unoffered tool: {name!r}")
        arguments_json = item.get("arguments_json")
        if not isinstance(arguments_json, str):
            raise ClaudeCLIMalformedOutputError("arguments_json must be a string")
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise ClaudeCLIMalformedOutputError("arguments_json is not valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ClaudeCLIMalformedOutputError("arguments_json must encode a JSON object")
        calls.append((str(name), arguments))

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost = payload.get("total_cost_usd")
    return ParsedTurn(
        text=text,
        tool_calls=tuple(calls),
        input_tokens=_int(usage, "input_tokens"),
        output_tokens=_int(usage, "output_tokens"),
        cache_creation_input_tokens=_int(usage, "cache_creation_input_tokens"),
        cache_read_input_tokens=_int(usage, "cache_read_input_tokens"),
        num_turns=_int(payload, "num_turns"),
        reported_estimate_usd=None if cost is None else str(cost),
    )


def build_cli_env(base: Mapping[str, str]) -> dict[str, str]:
    """Copy the environment without any API-key or endpoint overrides."""
    return {key: value for key, value in base.items() if key.upper() not in STRIPPED_ENV_VARS}


def build_cli_argv(
    *, binary: str, model: str, schema: Mapping[str, Any], mcp_config: Path
) -> list[str]:
    return [
        binary,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":"), sort_keys=True),
        "--tools",
        "",
        "--no-session-persistence",
        "--max-turns",
        str(CLI_MAX_TURNS),
        "--model",
        model,
        "--system-prompt",
        CLI_SYSTEM_PROMPT,
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
    ]


def verify_subscription_login(status: Mapping[str, Any]) -> dict[str, Any]:
    """Require the operator's claude.ai Pro login. Returns only non-identifying fields."""
    failures = []
    if status.get("loggedIn") is not True:
        failures.append("loggedIn")
    if status.get("authMethod") != REQUIRED_AUTH_METHOD:
        failures.append("authMethod")
    if status.get("subscriptionType") != REQUIRED_SUBSCRIPTION_TYPE:
        failures.append("subscriptionType")
    if status.get("apiProvider") != REQUIRED_API_PROVIDER:
        failures.append("apiProvider")
    if failures:
        raise ClaudeCLIAuthError(
            "claude login is not the required claude.ai Pro subscription; mismatched fields: "
            + ", ".join(failures)
        )
    return {
        "loggedIn": True,
        "authMethod": REQUIRED_AUTH_METHOD,
        "subscriptionType": REQUIRED_SUBSCRIPTION_TYPE,
        "apiProvider": REQUIRED_API_PROVIDER,
    }


def read_auth_status(binary: str, env: Mapping[str, str]) -> dict[str, Any]:
    """Read `claude auth status` (local credential check). Output is never echoed."""
    with tempfile.TemporaryDirectory(prefix="selfheal-claude-auth-") as tmp:
        try:
            # The binary is the operator-configured claude path, never repository content.
            completed = subprocess.run(  # noqa: S603
                [binary, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=CLI_AUTH_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
                cwd=tmp,
                env=dict(env),
            )
        except FileNotFoundError as exc:
            raise ClaudeCLINotFoundError(f"claude executable not found: {binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCLIAuthError("claude auth status timed out") from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCLIAuthError("claude auth status did not return JSON") from exc
    if not isinstance(payload, dict):
        raise ClaudeCLIAuthError("claude auth status did not return a JSON object")
    return payload


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _run_cli(
    argv: Sequence[str], prompt: str, *, cwd: Path, env: Mapping[str, str], limit_seconds: float
) -> tuple[str, str, int]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=dict(env),
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ClaudeCLINotFoundError(f"claude executable not found: {argv[0]}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")), timeout=limit_seconds
        )
    except TimeoutError as exc:
        _kill_process_group(process)
        await process.wait()
        raise ClaudeCLITimeoutError(f"claude CLI exceeded {limit_seconds:.0f} seconds") from exc
    return (
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
        process.returncode or 0,
    )


class ClaudeCLIProvider:
    """`messages.create` implementation backed by the local, subscription-logged-in CLI."""

    def __init__(self, *, binary: str, model: str, verified_login: Mapping[str, Any]) -> None:
        self._binary = binary
        self._model = model
        self._verified_login = dict(verified_login)
        self._calls: list[dict[str, Any]] = []

    @classmethod
    def from_verified_subscription(cls, *, binary: str, model: str) -> ClaudeCLIProvider:
        env = build_cli_env(os.environ)
        login = verify_subscription_login(read_auth_status(binary, env))
        login["api_key_env_present_and_stripped"] = "ANTHROPIC_API_KEY" in os.environ
        return cls(binary=binary, model=model, verified_login=login)

    @property
    def messages(self) -> ClaudeCLIProvider:
        return self

    async def create(self, **kwargs: Any) -> Response:
        system = system_text(kwargs.get("system"))
        messages = kwargs["messages"]
        tools = list(kwargs.get("tools") or [])
        if not tools:
            raise ClaudeCLIProviderError("tools are required; this adapter serves tool-call turns")
        tool_names = [str(tool["name"]) for tool in tools]
        requested = kwargs.get("request_timeout_seconds")
        timeout = (
            CLI_TIMEOUT_SECONDS if requested is None else min(float(requested), CLI_TIMEOUT_SECONDS)
        )
        prompt = render_transcript(system, messages, tools)
        schema = build_tool_call_schema(tool_names)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="selfheal-claude-cli-") as tmp:
            root = Path(tmp)
            workdir = root / "cwd"
            workdir.mkdir()
            mcp_config = root / "mcp.json"
            mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")
            argv = build_cli_argv(
                binary=self._binary, model=self._model, schema=schema, mcp_config=mcp_config
            )
            stdout, stderr, returncode = await _run_cli(
                argv,
                prompt,
                cwd=workdir,
                env=build_cli_env(os.environ),
                limit_seconds=timeout,
            )
        duration = round(time.monotonic() - started, 3)
        turn = parse_turn(stdout, stderr, returncode, tool_names)
        call_index = len(self._calls) + 1
        self._calls.append(
            {
                "index": call_index,
                "duration_seconds": duration,
                "num_turns": turn.num_turns,
                "input_tokens": turn.input_tokens,
                "output_tokens": turn.output_tokens,
                "cache_creation_input_tokens": turn.cache_creation_input_tokens,
                "cache_read_input_tokens": turn.cache_read_input_tokens,
                "tool_calls": [name for name, _ in turn.tool_calls],
                "reported_estimate_usd_unverified": turn.reported_estimate_usd,
            }
        )
        blocks: list[Block] = []
        if turn.text.strip():
            blocks.append(Block(type="text", text=turn.text))
        for index, (name, arguments) in enumerate(turn.tool_calls, start=1):
            blocks.append(
                Block(
                    type="tool_use", id=f"claude_{call_index}_{index}", name=name, input=arguments
                )
            )
        input_total = (
            turn.input_tokens + turn.cache_creation_input_tokens + turn.cache_read_input_tokens
        )
        return Response(
            content=blocks,
            usage=Usage(
                input_tokens=input_total,
                output_tokens=turn.output_tokens,
                cache_creation_input_tokens=turn.cache_creation_input_tokens,
                cache_read_input_tokens=turn.cache_read_input_tokens,
                cost_usd=None,
            ),
            stop_reason="tool_use" if turn.tool_calls else "end_turn",
        )

    def usage_summary(self) -> dict[str, Any]:
        """Evidence block. Subscription usage is recorded as unmetered and unverified."""
        estimates = [
            call["reported_estimate_usd_unverified"]
            for call in self._calls
            if call["reported_estimate_usd_unverified"] is not None
        ]
        total: str | None = None
        if estimates:
            try:
                total = str(sum(Decimal(value) for value in estimates))
            except InvalidOperation:
                total = None
        return {
            "metered": False,
            "cost_enforcement": "not_enforced_subscription_unmetered",
            "billed_cost_usd": None,
            "unverified_api_equivalent_estimate_usd": total,
            "cli_invocations": len(self._calls),
            "model": self._model,
            "verified_login": dict(self._verified_login),
            "calls": [dict(call) for call in self._calls],
        }
