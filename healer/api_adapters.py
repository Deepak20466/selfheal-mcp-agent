"""`gemini_api`/`groq_api`: real tool-calling backends via each provider's own
free-tier HTTP API (not a CLI, not the Anthropic SDK).

These are `AnthropicClientLike` ADAPTERS (see `healer/anthropic_client.py`'s
narrow structural Protocol) over Groq's OpenAI-compatible chat-completions
API and Google's Gemini `generateContent` function-calling API. Wrapping the
wire format this way -- rather than writing a second tool-call loop -- means
`healer/runtime_agent.py:run_heal_job` and `healer/ci_agent.py:
run_ci_heal_job` run **completely unchanged** for these two new backends:
every guardrail (sandboxed write scope, patch anti-cheat, budget caps,
circuit breakers, the fail-before-pass proof) is enforced by those same
modules exactly as it is for Claude/Codex/Gemini-CLI/API-mode. This was a
deliberate reuse decision, not an oversight -- rewriting the tool loop per
backend would each need its own re-verification of every guardrail.

Both raise `healer.backend_chain.BackendCooldownError` for a 429/401/403
response (quota, rate limit, or auth failure) so `healer/worker.py`'s chain
logic can mark that backend cooling down and move to the next one, and a
plain `RuntimeError` for anything else (a normal failure, per the task's
"other errors are normal failures").
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

from core.config import settings
from healer.backend_chain import BackendCooldownError
from sentinel.scrubber import scrub_text, scrub_value

GROQ_API_BASE = "https://api.groq.com/openai/v1"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_FREE_MODEL = "cohere/north-mini-code:free"
GROQ_API_DEFAULT_TIMEOUT_SECONDS = 120.0
GROQ_MAX_OUTPUT_TOKENS_PER_REQUEST = 4096
GROQ_MAX_CONTEXT_TOKENS = 131072
GROQ_TPM_LIMIT_TOKENS = 8000
GROQ_MIN_OUTPUT_TOKENS_PER_REQUEST = 256
GROQ_TPM_MAX_RETRY_AFTER_SECONDS = 60.0
GEMINI_API_DEFAULT_TIMEOUT_SECONDS = 120.0
_PROVIDER_RETRYABLE_STATUSES = frozenset({502, 503, 504})
_PROVIDER_MAX_ATTEMPTS = 3
_PROVIDER_MAX_RETRY_AFTER_SECONDS = 10.0
logger = structlog.get_logger(__name__)


class GeminiAPIRequestTimeout(RuntimeError):
    """The Gemini generateContent HTTP request exceeded its bounded timeout."""


class GroqAPIRequestTimeout(RuntimeError):
    """The Groq chat-completions HTTP request exceeded its bounded timeout."""


class GroqOutputTokenLimit(RuntimeError):
    """Groq ended a response at the adapter's output-token bound."""


class GroqRequestBudgetError(RuntimeError):
    """A Groq request cannot fit the configured context and TPM budgets."""


class GroqDailyQuotaError(BackendCooldownError):
    """A daily Groq quota was exhausted; benchmark resume must not retry it."""

    def __init__(self) -> None:
        self.daily_quota_exhausted = True
        super().__init__(
            "groq_api daily quota exhausted; no automatic retry was made",
            status_code=429,
        )


def _groq_rate_limit_scope(response: httpx.Response) -> str | None:
    if response.status_code != 429:
        return None
    try:
        error = response.json().get("error", {})
        message = error.get("message", "") if isinstance(error, dict) else ""
    except (ValueError, AttributeError):
        message = ""
    normalized = str(message).lower()
    if any(term in normalized for term in ("per day", "daily", "(rpd)", "(tpd)")):
        return "daily"
    if "tokens per minute" in normalized or "(tpm)" in normalized:
        return "tpm"
    return "unknown"


def _groq_tpm_retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if raw:
        try:
            seconds = float(raw)
            if seconds >= 0:
                return seconds
        except (OverflowError, ValueError):
            try:
                retry_at = parsedate_to_datetime(raw)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (OverflowError, TypeError, ValueError):
                pass
    try:
        error = response.json().get("error", {})
        message = error.get("message", "") if isinstance(error, dict) else ""
    except (ValueError, AttributeError):
        return None
    match = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*s\b", str(message), re.IGNORECASE)
    return float(match.group(1)) if match else None


def _groq_retry_delay_header_seconds(response: httpx.Response) -> float | None:
    """Return the provider's reset interval header when it is well formed."""
    raw = response.headers.get("x-ratelimit-reset-tokens", "").strip().lower()
    if not raw:
        return None
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", raw)
    if not match or not any(match.groups()):
        return None
    return float(match.group(1) or 0) * 60 + float(match.group(2) or 0)


def _groq_prompt_token_estimate(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    """Estimate request tokens without loading an unverified tokenizer.

    Two UTF-8 JSON bytes per estimated prompt token is deliberately more
    conservative than typical English/code token density; framing overhead is
    added separately. Groq's remaining-token headers are authoritative when
    available, and provider 429s remain the final signal for account-wide use
    outside this adapter. This is a budget estimate, not tokenizer telemetry.
    """
    encoded = json.dumps(
        {"messages": messages, "tools": tools}, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    framing_allowance = 32 * (len(messages) + len(tools) + 1)
    return (len(encoded) + 1) // 2 + framing_allowance


def _groq_compact_json(content: str, max_bytes: int = 8192) -> str:
    """Compact JSON tool output, visibly sampling only rediscoverable file lists."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return content
    if isinstance(value, list) and all(isinstance(path, str) for path in value):
        compact = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        if len(compact.encode("utf-8")) > max_bytes:
            # list_files accepts a glob, so omitted paths can be fetched again
            # on demand. Keep an evenly spaced sample and make omission explicit.
            sample_size = min(128, len(value))
            while sample_size >= 2:
                indexes = sorted(
                    {
                        round(index * (len(value) - 1) / (sample_size - 1))
                        for index in range(sample_size)
                    }
                )
                sampled = [value[index] for index in indexes]
                omitted = len(value) - len(sampled)
                sampled.append(
                    f"[Selfheal omitted {omitted} paths from this listing; call list_files with a "
                    "narrower glob to retrieve omitted paths]"
                )
                compact_sample = json.dumps(sampled, separators=(",", ":"), ensure_ascii=False)
                if len(compact_sample.encode("utf-8")) <= max_bytes:
                    return compact_sample
                sample_size //= 2
            return compact
        return compact
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


_GROQ_TEST_SIGNAL = re.compile(
    r"(?i)(::|\bFAILED\b|\bERRORS?\b|\bFAILURES?\b|\bpassed\b|\bfailed\b|"
    r"traceback|assertionerror|exception|^E\s|^>\s|^\s*File \"|"
    r"short test summary info|warning:|error:)",
    re.IGNORECASE | re.MULTILINE,
)


def _groq_compact_test_output(content: str, max_bytes: int) -> str | None:
    """Keep test names/errors/traceback lines; visibly mark omitted log noise.

    Return None if the diagnostic-bearing lines themselves cannot fit. The
    caller then fails closed instead of dropping repair evidence.
    """
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("output"), str):
        return None
    original = value["output"]
    if len(content.encode("utf-8")) <= max_bytes:
        return content
    lines = original.splitlines()
    if not lines:
        return None
    kept_indexes: set[int] = set()
    for index, line in enumerate(lines):
        if _GROQ_TEST_SIGNAL.search(line):
            kept_indexes.update(range(max(0, index - 2), min(len(lines), index + 3)))
    if not kept_indexes:
        # For a successful, verbose test run retain its leading invocation and
        # trailing summary; intermediate progress output is non-diagnostic.
        kept_indexes.update(range(min(8, len(lines))))
        kept_indexes.update(range(max(0, len(lines) - 24), len(lines)))
    retained = [lines[index] for index in sorted(kept_indexes)]
    omitted = len(lines) - len(kept_indexes)
    value["output"] = (
        "[Selfheal compacted test output; "
        f"{omitted} non-selected lines omitted; complete output remains in local tool history]\n"
        + "\n".join(retained)
    )
    compacted = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return compacted if len(compacted.encode("utf-8")) <= max_bytes else None


def _groq_compact_read_file(content: str, max_bytes: int) -> str:
    """Compact oversized source reads with explicit, line-addressable omissions.

    `read_file` supports start_line/end_line, so omitted source remains
    retrievable by the model. The original tool history is never modified;
    this only shapes a copy of the outgoing provider request.
    """
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(value, dict) or not isinstance(value.get("content"), str):
        return content
    if len(content.encode("utf-8")) <= max_bytes:
        return content

    lines = value["content"].splitlines()
    start_line = value.get("start_line", 1)
    if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
        start_line = 1
    for keep_per_side in (64, 32, 16, 8, 4, 2, 1, 0):
        if len(lines) <= keep_per_side * 2:
            continue
        omitted_start = start_line + keep_per_side
        omitted_end = start_line + len(lines) - keep_per_side - 1
        selected = lines[:keep_per_side] + [
            f"[Selfheal omitted source lines {omitted_start}-{omitted_end}; "
            "retrieve them with read_file start_line/end_line]"
        ]
        if keep_per_side:
            selected.extend(lines[-keep_per_side:])
        compacted_value = dict(value)
        compacted_value["content"] = "\n".join(selected)
        compacted = json.dumps(compacted_value, separators=(",", ":"), ensure_ascii=False)
        if len(compacted.encode("utf-8")) <= max_bytes:
            return compacted
    return content


def _groq_compact_error(content: str, max_bytes: int) -> str | None:
    """Compact oversized error records without dropping repair diagnostics.

    The exception identity, message, source location, and traceback boundaries
    are retained. The complete record remains in local tool history; omitted
    traceback lines can be fetched again with ``get_error``.
    """
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("traceback"), str):
        return None

    traceback_lines = value["traceback"].splitlines()
    for keep_per_side in (64, 32, 16, 8, 4, 2, 1, 0):
        if len(traceback_lines) <= keep_per_side * 2:
            continue
        selected = traceback_lines[:keep_per_side]
        selected.append(
            "[Selfheal omitted traceback lines; the complete traceback remains in local "
            "tool history and can be fetched again with get_error]"
        )
        if keep_per_side:
            selected.extend(traceback_lines[-keep_per_side:])
        compacted_value = dict(value)
        compacted_value["traceback"] = "\n".join(selected)
        compacted = json.dumps(compacted_value, separators=(",", ":"), ensure_ascii=False)
        if len(compacted.encode("utf-8")) <= max_bytes:
            return compacted

    # Error records may contain a single very long traceback line. Keep the
    # diagnostic fields intact and compact only that line by UTF-8 bytes.
    compacted_value = dict(value)
    compacted_value["traceback"] = (
        "[Selfheal compacted traceback; full traceback remains in local tool history and "
        "can be fetched again with get_error]"
    )
    compacted = json.dumps(compacted_value, separators=(",", ":"), ensure_ascii=False)
    return compacted if len(compacted.encode("utf-8")) <= max_bytes else None


OPENROUTER_TOOL_RESULT_MAX_BYTES = 8_192
OPENROUTER_DIAGNOSTIC_PREVIEW_MAX_BYTES = 768


def _openrouter_safe_call_id(value: Any) -> str:
    """Keep provider call IDs useful for correlation without logging control data."""
    if not isinstance(value, str) or not value:
        return "missing"
    safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", value)
    if len(safe) > 96:
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"{safe[:80]}-{digest}"
    return safe


def _openrouter_diagnostic_preview(content: str) -> str:
    """Return a small, scrubbed snapshot useful for offline adapter reproduction."""
    try:
        value = scrub_value(json.loads(content))
        rendered = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = json.dumps(scrub_text(content), ensure_ascii=False)
    # This serialized preview passes through the global log scrubber a second
    # time as free text. Escaping @ keeps an escaped JSON newline before a
    # decorator (``\\n@router.put``) from looking like an email address there.
    # The JSON value decodes to the same already-scrubbed preview.
    rendered = rendered.replace("@", "\\u0040")
    encoded = rendered.encode("utf-8")
    if len(encoded) <= OPENROUTER_DIAGNOSTIC_PREVIEW_MAX_BYTES:
        return rendered
    suffix = b"...[truncated]"
    prefix = encoded[: OPENROUTER_DIAGNOSTIC_PREVIEW_MAX_BYTES - len(suffix)]
    return prefix.decode("utf-8", errors="ignore") + suffix.decode("ascii")


def _compact_openrouter_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound rediscoverable MCP results on an outgoing copy of chat history."""
    compacted = copy.deepcopy(messages)
    # MCP tool results belong to the immediately preceding assistant tool-call
    # turn. Do not build one global ID map: providers may reuse call IDs across
    # turns, which otherwise associates an older result with a newer tool name.
    call_names: dict[str, tuple[str, str]] = {}
    turn_index = -1
    for message_index, message in enumerate(compacted):
        if message.get("role") == "assistant":
            turn_index += 1
            call_names = {
                call["id"]: (
                    call["function"]["name"],
                    f"turn-{turn_index}:call-{call_index}",
                )
                for call_index, call in enumerate(message.get("tool_calls", []))
                if isinstance(call, dict)
                and isinstance(call.get("id"), str)
                and isinstance(call.get("function"), dict)
                and isinstance(call["function"].get("name"), str)
            }
            continue
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        original_content = content
        original_bytes = len(original_content.encode("utf-8"))
        call_id = message.get("tool_call_id")
        call_info = call_names.get(call_id) if isinstance(call_id, str) else None
        raw_tool_name, call_key = call_info if call_info is not None else (None, "unmatched")
        # Names originate in provider history; only known MCP names may enter
        # diagnostic fields. Unknown names are represented by a fixed label.
        tool_name = (
            raw_tool_name
            if raw_tool_name
            in {"get_error", "read_file", "list_files", "run_tests", "propose_patch"}
            else "unknown"
        )
        outcome = (
            "not_needed" if original_bytes <= OPENROUTER_TOOL_RESULT_MAX_BYTES else "compacted"
        )
        failure_category: str | None = None
        if original_bytes > OPENROUTER_TOOL_RESULT_MAX_BYTES:
            if call_info is None:
                outcome = "failed"
                failure_category = "unmatched_call_id"
            elif tool_name == "list_files":
                content = _groq_compact_json(content, OPENROUTER_TOOL_RESULT_MAX_BYTES)
            elif tool_name == "read_file":
                content = _groq_compact_read_file(content, OPENROUTER_TOOL_RESULT_MAX_BYTES)
            elif tool_name == "get_error":
                content = _groq_compact_error(content, OPENROUTER_TOOL_RESULT_MAX_BYTES) or content
            elif tool_name == "run_tests":
                content = _groq_compact_test_output(content, OPENROUTER_TOOL_RESULT_MAX_BYTES)
                if content is None:
                    outcome = "failed"
                    failure_category = "test_evidence_not_safely_compactable"
            else:
                outcome = "failed"
                failure_category = "unsupported_tool"

            if (
                outcome != "failed"
                and isinstance(content, str)
                and len(content.encode("utf-8")) > OPENROUTER_TOOL_RESULT_MAX_BYTES
            ):
                outcome = "failed"
                failure_category = "context_limit_after_compaction"

        result_bytes = len(content.encode("utf-8")) if isinstance(content, str) else None
        logger.info(
            "provider.tool_result_diagnostic",
            backend="openrouter_api",
            tool=tool_name,
            call_id=_openrouter_safe_call_id(call_id),
            call_key=call_key,
            message_index=message_index,
            payload_bytes=original_bytes,
            outgoing_bytes=result_bytes,
            compaction_outcome=outcome,
            failure_category=failure_category,
            payload_sha256=hashlib.sha256(original_content.encode("utf-8")).hexdigest(),
            payload_preview=_openrouter_diagnostic_preview(original_content),
        )

        if outcome == "failed":
            if failure_category == "test_evidence_not_safely_compactable":
                raise RuntimeError(
                    "OpenRouter tool output could not be compacted without losing test evidence"
                )
            raise RuntimeError(
                "OpenRouter tool output exceeds the safe context limit "
                f"(tool={tool_name}, bytes={original_bytes}, category={failure_category})"
            )
        message["content"] = content
        if isinstance(call_id, str):
            call_names.pop(call_id, None)
    return compacted


@dataclass
class _Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    gemini_part: dict[str, Any] | None = field(default=None, repr=False)
    gemini_call_id: str | None = None


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: Decimal | None = None


def _usage_cost(usage: Any) -> Decimal | None:
    raw = usage.get("cost") if isinstance(usage, dict) else None
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage
    stop_reason: str = "end_turn"


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    parts = []
    for block in system or []:
        text = block.get("text") if isinstance(block, dict) else None
        if text:
            parts.append(text)
    return "\n".join(parts)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0, int(float(raw)))
    except (OverflowError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0, int((retry_at - datetime.now(UTC)).total_seconds()))
        except (OverflowError, TypeError, ValueError):
            return None


def _raise_for_status_classified(response: httpx.Response, *, backend: str) -> None:
    if response.status_code in (401, 403, 429):
        raise BackendCooldownError(
            f"{backend} returned {response.status_code}: {response.text[:500]}",
            retry_after_seconds=_retry_after_seconds(response),
            status_code=response.status_code,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"{backend} returned {response.status_code}: {response.text[:500]}")


async def _provider_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    backend: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    request_timeout_seconds: float | None = None,
) -> httpx.Response:
    """Retry explicit upstream gateway failures with bounded jitter.

    Rate limits and transport errors are not replayed: a transport failure may
    happen after an inference was accepted, and 429 responses use cooldown and
    backend fallback instead of repeating a potentially billable request.
    """
    for attempt in range(_PROVIDER_MAX_ATTEMPTS):
        if request_timeout_seconds is None:
            response = await client.post(url, headers=headers, json=payload)
        else:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=request_timeout_seconds,
            )
        if response.status_code not in _PROVIDER_RETRYABLE_STATUSES:
            return response
        if attempt + 1 >= _PROVIDER_MAX_ATTEMPTS:
            return response

        retry_after = _retry_after_seconds(response)
        if retry_after is not None and retry_after > _PROVIDER_MAX_RETRY_AFTER_SECONDS:
            return response
        ceiling = min(2.0, 0.25 * (2**attempt))
        jitter = random.SystemRandom().uniform(0.0, ceiling)
        delay = max(0, retry_after or 0) + jitter
        logger.warning(
            "provider.request.retry",
            backend=backend,
            attempt=attempt + 1,
            status_code=response.status_code,
            delay_seconds=round(delay, 3),
        )
        await asyncio.sleep(delay)
    raise AssertionError("provider retry loop did not return a response")


class _GroqMessages:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._tpm_usage: list[tuple[float, int]] = []
        self._server_remaining_tokens: int | None = None
        self._server_remaining_observed_at: float | None = None
        self._server_token_reset_at: float | None = None

    def _available_tpm_tokens(self, now: float) -> int:
        self._tpm_usage = [(at, count) for at, count in self._tpm_usage if now - at < 60]
        if self._server_token_reset_at is not None and now >= self._server_token_reset_at:
            self._server_remaining_tokens = GROQ_TPM_LIMIT_TOKENS
            self._server_remaining_observed_at = now
            self._server_token_reset_at = None
        if self._server_remaining_tokens is not None:
            observed_at = self._server_remaining_observed_at or now
            after_observation = sum(count for at, count in self._tpm_usage if at > observed_at)
            return max(
                0, min(GROQ_TPM_LIMIT_TOKENS, self._server_remaining_tokens - after_observation)
            )
        locally_used = sum(count for _, count in self._tpm_usage)
        return max(0, GROQ_TPM_LIMIT_TOKENS - locally_used)

    def _record_groq_usage(self, response: httpx.Response, usage: dict[str, Any]) -> None:
        now = time.monotonic()
        charged = max(0, int(usage.get("prompt_tokens", 0) or 0)) + max(
            0, int(usage.get("completion_tokens", 0) or 0)
        )
        self._tpm_usage.append((now, charged))
        raw_remaining = response.headers.get("x-ratelimit-remaining-tokens")
        if raw_remaining is not None:
            try:
                self._server_remaining_tokens = min(
                    GROQ_TPM_LIMIT_TOKENS, max(0, int(raw_remaining))
                )
            except ValueError:
                return
            self._server_remaining_observed_at = now
            reset_after = _groq_retry_delay_header_seconds(response)
            self._server_token_reset_at = now + reset_after if reset_after else None

    def _set_tpm_state_from_error(self, response: httpx.Response) -> tuple[int, int, int] | None:
        try:
            error = response.json().get("error", {})
            message = error.get("message", "") if isinstance(error, dict) else ""
        except (ValueError, AttributeError):
            return None
        match = re.search(
            r"(?i)tokens per minute.*?limit\s+(\d+).*?used\s+(\d+).*?requested\s+(\d+)",
            str(message),
        )
        limit = GROQ_TPM_LIMIT_TOKENS
        used = -1
        requested = -1
        if match:
            limit = min(GROQ_TPM_LIMIT_TOKENS, int(match.group(1)))
            used = int(match.group(2))
            requested = int(match.group(3))
            self._server_remaining_tokens = max(0, limit - used)
            self._server_remaining_observed_at = time.monotonic()
        return (limit, used, requested) if match else None

    def _prepare_groq_messages(
        self,
        system: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        requested_max_tokens: int,
        available_tpm_override: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
        converted_tools = self._to_openai_tools(tools)
        base_messages = self._to_openai_messages(system, messages)
        call_names = {
            call["id"]: call["function"]["name"]
            for message in base_messages
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        }
        requested_output = min(int(requested_max_tokens), GROQ_MAX_OUTPUT_TOKENS_PER_REQUEST)
        available_tpm = (
            self._available_tpm_tokens(time.monotonic())
            if available_tpm_override is None
            else min(GROQ_TPM_LIMIT_TOKENS, max(0, available_tpm_override))
        )
        compression_levels = [8192, 4096, 2048, 1024, 512, 256, 128]
        last_prompt_bound = 0
        for output_limit in compression_levels:
            candidate = copy.deepcopy(base_messages)
            for message in candidate:
                if message.get("role") != "tool":
                    continue
                tool_name = call_names.get(message.get("tool_call_id"))
                if tool_name == "list_files":
                    message["content"] = _groq_compact_json(
                        str(message.get("content", "")), output_limit
                    )
                elif tool_name == "read_file":
                    message["content"] = _groq_compact_read_file(
                        str(message.get("content", "")), output_limit
                    )
                elif tool_name == "run_tests":
                    compacted = _groq_compact_test_output(
                        str(message.get("content", "")), output_limit
                    )
                    if compacted is not None:
                        message["content"] = compacted
            prompt_bound = _groq_prompt_token_estimate(candidate, converted_tools)
            last_prompt_bound = prompt_bound
            output_tokens = min(
                requested_output,
                GROQ_MAX_CONTEXT_TOKENS - prompt_bound,
                available_tpm - prompt_bound,
            )
            minimum_required = min(requested_output, GROQ_MIN_OUTPUT_TOKENS_PER_REQUEST)
            if output_tokens >= minimum_required and output_tokens > 0:
                return candidate, converted_tools, prompt_bound, output_tokens
        logger.warning(
            "provider.request.rejected_by_local_budget",
            backend="groq_api",
            prompt_token_estimate=last_prompt_bound,
            available_tpm_tokens=available_tpm,
            context_limit_tokens=GROQ_MAX_CONTEXT_TOKENS,
            requested_output_tokens=requested_output,
            minimum_output_tokens=min(requested_output, GROQ_MIN_OUTPUT_TOKENS_PER_REQUEST),
            full_tool_history_preserved=True,
        )
        raise GroqRequestBudgetError(
            "Groq request not sent: prompt/tool history cannot fit the current TPM/context "
            f"budget (prompt_estimate={last_prompt_bound}, available_tpm={available_tpm}, "
            f"context_limit={GROQ_MAX_CONTEXT_TOKENS}, requested_output={requested_output}); "
            "only list-files JSON, read-file source ranges, and non-diagnostic test-output "
            "lines were compacted"
        )

    def _to_openai_messages(
        self, system: Any, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": _system_text(system)}]
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text_parts = []
                tool_calls = []
                for block in content:
                    block_type = block.type if isinstance(block, _Block) else block.get("type")
                    if block_type == "text":
                        text = block.text if isinstance(block, _Block) else block.get("text")
                        if text:
                            text_parts.append(text)
                    elif block_type == "tool_use":
                        block_id = block.id if isinstance(block, _Block) else block.get("id")
                        name = block.name if isinstance(block, _Block) else block.get("name")
                        args = block.input if isinstance(block, _Block) else block.get("input", {})
                        tool_calls.append(
                            {
                                "id": block_id,
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                        )
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) or None,
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                out.append(assistant_msg)
            else:
                # A "user" turn whose content is a list of tool_result blocks
                # (Anthropic shape) -> one OpenAI "tool" message per result.
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": item["tool_use_id"],
                                "content": str(item.get("content", "")),
                            }
                        )
                    elif isinstance(item, dict) and item.get("type") == "text":
                        out.append({"role": "user", "content": item.get("text", "")})
        # Privacy guard, defense-in-depth (terminal-only v1.0 Step 5): scrub
        # every plain-text message content field right before it goes out
        # over the wire to the compatible provider. Tool RESULTS are already scrubbed once at
        # the MCP layer (mcp_server/audit.py) -- this is a second, cheap
        # pass on the final HTTP payload, not the only line of defense.
        for m in out:
            if isinstance(m.get("content"), str):
                content = m["content"]
                # MCP tools commonly return JSON as text. Scrubbing that wire
                # representation with regexes can match across JSON escapes:
                # for example, ``\\n@router.post`` contains ``n@router``
                # which looks like an email address and becomes invalid JSON.
                # Parse and scrub JSON values instead, then serialize them
                # again. This preserves source text such as Python decorators
                # while retaining the same secret and PII redaction policy.
                if m.get("role") == "tool":
                    try:
                        content = json.dumps(
                            scrub_value(json.loads(content)),
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                    except (TypeError, ValueError):
                        content = scrub_text(content)
                else:
                    content = scrub_text(content)
                m["content"] = content
        return out

    @staticmethod
    def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    async def create(self, **kwargs: Any) -> _Response:
        system = kwargs.get("system")
        messages = kwargs["messages"]
        tools = kwargs.get("tools", [])
        request_timeout_seconds = kwargs.pop("request_timeout_seconds", None)
        if request_timeout_seconds is None:
            request_deadline = time.monotonic() + GROQ_API_DEFAULT_TIMEOUT_SECONDS
        elif (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be a positive number")
        else:
            request_deadline = time.monotonic() + float(request_timeout_seconds)
        requested_max_tokens = int(kwargs.get("max_tokens", GROQ_MAX_OUTPUT_TOKENS_PER_REQUEST))
        if requested_max_tokens <= 0:
            raise ValueError("max_tokens must be a positive number")
        try:
            groq_messages, groq_tools, prompt_bound, max_tokens = self._prepare_groq_messages(
                system, messages, tools, requested_max_tokens
            )
        except GroqRequestBudgetError as current_budget_error:
            try:
                self._prepare_groq_messages(
                    system,
                    messages,
                    tools,
                    requested_max_tokens,
                    available_tpm_override=GROQ_TPM_LIMIT_TOKENS,
                )
            except GroqRequestBudgetError as fresh_budget_error:
                raise GroqRequestBudgetError(
                    "Groq request not sent: prompt/tool history cannot fit a fresh "
                    f"{GROQ_TPM_LIMIT_TOKENS}-token TPM window; {fresh_budget_error}"
                ) from current_budget_error

            now = time.monotonic()
            reset_at = self._server_token_reset_at
            wait_seconds = reset_at - now if reset_at is not None else None
            remaining_request_seconds = request_deadline - now
            if wait_seconds is None or wait_seconds <= 0:
                raise GroqRequestBudgetError(
                    "Groq request not sent: it can fit a fresh TPM window but the server "
                    "did not provide a usable token-reset time; "
                    f"{current_budget_error}"
                ) from current_budget_error
            if wait_seconds > GROQ_TPM_MAX_RETRY_AFTER_SECONDS:
                raise GroqRequestBudgetError(
                    "Groq request not sent: it can fit a fresh TPM window but the server "
                    f"reset delay exceeds the {GROQ_TPM_MAX_RETRY_AFTER_SECONDS:g}-second "
                    f"wait bound; remaining deadline={remaining_request_seconds:.3f}s"
                ) from current_budget_error
            if wait_seconds + 1.0 >= remaining_request_seconds:
                raise GroqAPIRequestTimeout(
                    "Groq TPM reset wait would exhaust the remaining request deadline"
                ) from current_budget_error

            logger.warning(
                "provider.request.waiting_for_tpm_reset",
                backend="groq_api",
                current_available_tpm_tokens=self._available_tpm_tokens(now),
                fresh_tpm_limit_tokens=GROQ_TPM_LIMIT_TOKENS,
                reset_wait_seconds=round(wait_seconds, 3),
            )
            await asyncio.sleep(wait_seconds)
            if request_deadline - time.monotonic() <= 0:
                raise GroqAPIRequestTimeout(
                    "Groq TPM reset wait exhausted the remaining request deadline"
                ) from current_budget_error
            # The server's reset header is the signal for a replenished window.
            self._server_remaining_tokens = GROQ_TPM_LIMIT_TOKENS
            self._server_remaining_observed_at = time.monotonic()
            self._server_token_reset_at = None
            groq_messages, groq_tools, prompt_bound, max_tokens = self._prepare_groq_messages(
                system, messages, tools, requested_max_tokens
            )
        remaining_request_seconds = request_deadline - time.monotonic()
        if remaining_request_seconds <= 0:
            raise GroqAPIRequestTimeout("Groq request deadline expired during local preflight")
        request_timeout = httpx.Timeout(remaining_request_seconds)

        payload: dict[str, Any] = {
            "model": settings.groq_api_model,
            "messages": groq_messages,
            "max_tokens": max_tokens,
            # The default model (openai/gpt-oss-120b) is a reasoning model:
            # verified live that with the default (higher) reasoning effort
            # it can burn the entire max_tokens budget on its hidden
            # `reasoning` field and return empty `content` (finish_reason
            # "length") -- "low" keeps that overhead small and predictable
            # for a tool-calling agent loop.
            "reasoning_effort": "low",
        }
        if groq_tools:
            payload["tools"] = groq_tools
            payload["tool_choice"] = "auto"

        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await _provider_post(
                    client,
                    f"{GROQ_API_BASE}/chat/completions",
                    backend="groq_api",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    payload=payload,
                )
                scope = _groq_rate_limit_scope(response)
                if scope == "daily":
                    raise GroqDailyQuotaError()
                retry_after = _groq_tpm_retry_after_seconds(response) if scope == "tpm" else None
                counters = self._set_tpm_state_from_error(response) if scope == "tpm" else None
                remaining_seconds = request_deadline - time.monotonic()
                if (
                    scope == "tpm"
                    and retry_after is not None
                    and retry_after <= GROQ_TPM_MAX_RETRY_AFTER_SECONDS
                    and retry_after + 1.0 < remaining_seconds
                ):
                    logger.warning(
                        "provider.request.retry",
                        backend="groq_api",
                        attempt=1,
                        status_code=429,
                        reason="temporary_tpm_limit",
                        tpm_limit_tokens=counters[0] if counters else GROQ_TPM_LIMIT_TOKENS,
                        tpm_used_tokens=counters[1] if counters else None,
                        tpm_requested_tokens=counters[2] if counters else None,
                        delay_seconds=round(retry_after, 3),
                    )
                    await asyncio.sleep(retry_after)
                    remaining_seconds = request_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        raise GroqAPIRequestTimeout(
                            "Groq TPM retry delay exhausted the remaining request deadline"
                        )
                    # The provider's retry-after response is the reset signal;
                    # retry one bounded request against a fresh 8K window.
                    self._server_remaining_tokens = GROQ_TPM_LIMIT_TOKENS
                    self._server_remaining_observed_at = time.monotonic()
                    self._server_token_reset_at = None
                    response = await _provider_post(
                        client,
                        f"{GROQ_API_BASE}/chat/completions",
                        backend="groq_api",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        payload=payload,
                        request_timeout_seconds=remaining_seconds,
                    )
                    if _groq_rate_limit_scope(response) == "daily":
                        raise GroqDailyQuotaError()
                elif scope == "tpm":
                    logger.warning(
                        "provider.request.not_retried",
                        backend="groq_api",
                        reason=(
                            "retry_delay_missing_or_exceeds_bound"
                            if retry_after is None or retry_after > GROQ_TPM_MAX_RETRY_AFTER_SECONDS
                            else "retry_delay_exceeds_remaining_deadline"
                        ),
                        retry_after_seconds=retry_after,
                        remaining_deadline_seconds=max(0.0, remaining_seconds),
                        tpm_limit_tokens=counters[0] if counters else GROQ_TPM_LIMIT_TOKENS,
                        tpm_used_tokens=counters[1] if counters else None,
                        tpm_requested_tokens=counters[2] if counters else None,
                    )
        except httpx.TimeoutException as exc:
            raise GroqAPIRequestTimeout(f"Groq chat/completions HTTP {type(exc).__name__}") from exc
        if response.status_code == 400 and any(
            phrase in response.text.lower()
            for phrase in ("reduce the length", "context length", "too many tokens")
        ):
            logger.warning(
                "provider.request.rejected_for_length",
                backend="groq_api",
                prompt_token_estimate=prompt_bound,
                completion_token_reservation=max_tokens,
                context_limit_tokens=GROQ_MAX_CONTEXT_TOKENS,
                tpm_limit_tokens=GROQ_TPM_LIMIT_TOKENS,
            )
            raise GroqRequestBudgetError(
                "Groq rejected request length (HTTP 400) after local preflight; "
                f"prompt_estimate={prompt_bound}, completion_reservation={max_tokens}, "
                f"context_limit={GROQ_MAX_CONTEXT_TOKENS}, tpm_limit={GROQ_TPM_LIMIT_TOKENS}"
            )
        _raise_for_status_classified(response, backend="groq_api")
        body = response.json()
        usage = body.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        self._record_groq_usage(response, usage)

        choice = body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise GroqOutputTokenLimit(
                "Groq response reached the output-token cap; prompt and tool history were preserved"
            )
        message = choice["message"]
        blocks: list[_Block] = []
        if message.get("content"):
            blocks.append(_Block(type="text", text=message["content"]))
        for call in message.get("tool_calls") or []:
            try:
                args = json.loads(call["function"]["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append(
                _Block(type="tool_use", id=call["id"], name=call["function"]["name"], input=args)
            )

        return _Response(
            content=blocks,
            usage=_Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cost_usd=_usage_cost(usage),
            ),
        )


class GroqClient:
    """`AnthropicClientLike` -- pass to `runtime_agent.run_heal_job`/
    `ci_agent.run_ci_heal_job` exactly like the real Anthropic client."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.groq_api_key
        if not key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        self._messages = _GroqMessages(key)

    @property
    def messages(self) -> _GroqMessages:
        return self._messages


class _OpenRouterMessages(_GroqMessages):
    """Same OpenAI-compatible chat-completions wire format as Groq (subclass
    reuses `_GroqMessages._to_openai_messages`/`_to_openai_tools` unchanged
    -- OpenRouter's `/chat/completions` shape is byte-for-byte the same
    request/response shape Groq's is), pointed at OpenRouter's endpoint and
    model instead. Deliberately reuses `settings.anthropic_api_key`/
    `settings.anthropic_model` rather than adding new `OPENROUTER_*`
    settings: this project's `.env` already configures an OpenRouter key via
    those two fields (they were already there for `api`-mode's
    `ANTHROPIC_BASE_URL` override), and `openrouter_api` just reads the same
    values -- no reason to duplicate the setting under a second name.
    """

    async def create(self, **kwargs: Any) -> _Response:
        if settings.anthropic_model != OPENROUTER_FREE_MODEL:
            raise RuntimeError(
                f"OpenRouter is restricted to the allowlisted free model {OPENROUTER_FREE_MODEL}"
            )
        system = kwargs.get("system")
        messages = kwargs["messages"]
        tools = kwargs.get("tools", [])
        max_tokens = kwargs.get("max_tokens", 4096)

        payload: dict[str, Any] = {
            "model": settings.anthropic_model,
            "messages": _compact_openrouter_tool_messages(
                self._to_openai_messages(system, messages)
            ),
            "max_tokens": max_tokens,
            # `openrouter/free` (and other free OpenRouter models) are often
            # reasoning models -- verified live this session that a small
            # max_tokens budget (e.g. this project's own live-check probe,
            # 64 tokens) can be spent entirely on the hidden `reasoning`
            # field, returning empty `content` -- same failure mode already
            # documented and fixed for groq_api's default model above.
            # "low" keeps that overhead small and predictable for a
            # tool-calling agent loop; OpenRouter passes it through to
            # whichever underlying model supports it, ignored otherwise.
            "reasoning_effort": "low",
            # VIMJ's free-model route must not move to a paid model if the
            # selected free endpoint is unavailable. The benchmark pins the
            # model ID and verifies its live zero-price metadata before use.
            "provider": {"allow_fallbacks": False},
            "usage": {"include": True},
        }
        if tools:
            payload["tools"] = self._to_openai_tools(tools)
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=120.0) as client:
            # Do not replay a completion: a gateway timeout can occur after
            # inference was accepted, and the OpenRouter free daily allowance
            # counts failed attempts too.
            response = await client.post(
                f"{OPENROUTER_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        _raise_for_status_classified(response, backend="openrouter_api")
        body = response.json()

        if body.get("model") != settings.anthropic_model:
            raise RuntimeError("OpenRouter returned a model other than the configured free model")

        message = body["choices"][0]["message"]
        blocks: list[_Block] = []
        if message.get("content"):
            blocks.append(_Block(type="text", text=message["content"]))
        for call in message.get("tool_calls") or []:
            try:
                args = json.loads(call["function"]["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append(
                _Block(type="tool_use", id=call["id"], name=call["function"]["name"], input=args)
            )

        usage = body.get("usage", {})
        if (
            not isinstance(usage, dict)
            or usage.get("is_byok") is not False
            or _usage_cost(usage) != Decimal("0")
        ):
            raise RuntimeError("OpenRouter did not verify a zero-cost, non-BYOK completion")
        return _Response(
            content=blocks,
            usage=_Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                cost_usd=_usage_cost(usage),
            ),
        )


class OpenRouterClient:
    """`AnthropicClientLike` adapter over OpenRouter's OpenAI-compatible
    `/chat/completions` API. It pins the exact VIMJ allowlisted :free model,
    verifies live endpoint pricing and function-calling metadata,
    disables provider fallback, and requires every response to report zero
    cost for that exact model.
    """

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("OPENROUTER_API_KEY") or settings.anthropic_api_key
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY or ANTHROPIC_API_KEY is not configured")
        self._messages = _OpenRouterMessages(key)

    @property
    def messages(self) -> _OpenRouterMessages:
        return self._messages


class _GeminiMessages:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _to_gemini_contents(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending_calls: dict[str, tuple[str, str | None]] = {}
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                out.append(
                    {"role": "user" if role == "user" else "model", "parts": [{"text": content}]}
                )
                continue
            if role == "assistant":
                parts: list[dict[str, Any]] = []
                pending_calls = {}
                for block in content:
                    block_type = block.type if isinstance(block, _Block) else block.get("type")
                    raw_part = (
                        block.gemini_part if isinstance(block, _Block) else block.get("gemini_part")
                    )
                    if isinstance(raw_part, dict):
                        # Gemini 3 thought signatures are opaque, model-generated
                        # values. Return each original Part byte-for-byte at the
                        # JSON-value level and in its original position.
                        parts.append(copy.deepcopy(raw_part))
                        if block_type == "tool_use":
                            block_id = block.id if isinstance(block, _Block) else block.get("id")
                            name = block.name if isinstance(block, _Block) else block.get("name")
                            call_id = (
                                block.gemini_call_id
                                if isinstance(block, _Block)
                                else block.get("gemini_call_id")
                            )
                            if isinstance(block_id, str) and isinstance(name, str):
                                pending_calls[block_id] = (
                                    name,
                                    call_id if isinstance(call_id, str) else None,
                                )
                        continue
                    if block_type == "text":
                        text = block.text if isinstance(block, _Block) else block.get("text")
                        if text:
                            parts.append({"text": text})
                    elif block_type == "tool_use":
                        block_id = block.id if isinstance(block, _Block) else block.get("id")
                        name = block.name if isinstance(block, _Block) else block.get("name")
                        args = block.input if isinstance(block, _Block) else block.get("input", {})
                        parts.append({"functionCall": {"name": name, "args": args}})
                        if isinstance(block_id, str) and isinstance(name, str):
                            call_id = (
                                block.gemini_call_id
                                if isinstance(block, _Block)
                                else block.get("gemini_call_id")
                            )
                            pending_calls[block_id] = (
                                name,
                                call_id if isinstance(call_id, str) else None,
                            )
                out.append({"role": "model", "parts": parts})
            else:
                parts = []
                result_items = {
                    item.get("tool_use_id", ""): item
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "tool_result"
                }
                if result_items.keys() - pending_calls.keys():
                    raise ValueError("Gemini function response has no matching function call")
                for block_id, (name, call_id) in pending_calls.items():
                    item = result_items.get(block_id)
                    if item is None:
                        raise ValueError("Gemini function call has no matching function response")
                    function_response: dict[str, Any] = {
                        "name": name,
                        "response": {"content": str(item.get("content", ""))},
                    }
                    if call_id is not None:
                        function_response["id"] = call_id
                    parts.append({"functionResponse": function_response})
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append({"text": item.get("text", "")})
                out.append({"role": "user", "parts": parts})
                pending_calls = {}
        # Privacy guard, defense-in-depth (terminal-only v1.0 Step 5): see
        # the matching comment in _GroqMessages._to_openai_messages above.
        for m in out:
            for part in m.get("parts", []):
                if isinstance(part.get("text"), str):
                    part["text"] = scrub_text(part["text"])
                response = part.get("functionResponse", {}).get("response")
                if isinstance(response, dict) and isinstance(response.get("content"), str):
                    response["content"] = scrub_text(response["content"])
        return out

    @staticmethod
    def _to_gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        declarations = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]
        return [{"functionDeclarations": declarations}]

    async def create(self, **kwargs: Any) -> _Response:
        system = kwargs.get("system")
        messages = kwargs["messages"]
        tools = kwargs.get("tools", [])
        max_tokens = kwargs.get("max_tokens", 4096)
        request_timeout_seconds = kwargs.pop("request_timeout_seconds", None)
        if request_timeout_seconds is None:
            request_timeout: float | httpx.Timeout = GEMINI_API_DEFAULT_TIMEOUT_SECONDS
        elif (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or request_timeout_seconds <= 0
        ):
            raise ValueError("request_timeout_seconds must be a positive number")
        else:
            request_timeout = httpx.Timeout(float(request_timeout_seconds))

        payload: dict[str, Any] = {
            "contents": self._to_gemini_contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        system_text = scrub_text(_system_text(system))
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            payload["tools"] = self._to_gemini_tools(tools)

        try:
            async with httpx.AsyncClient(timeout=request_timeout) as client:
                response = await _provider_post(
                    client,
                    f"{GEMINI_API_BASE}/models/{settings.gemini_api_model}:generateContent",
                    backend="gemini_api",
                    headers={"x-goog-api-key": self._api_key},
                    payload=payload,
                )
        except httpx.TimeoutException as exc:
            raise GeminiAPIRequestTimeout(
                f"Gemini generateContent HTTP {type(exc).__name__}"
            ) from exc
        _raise_for_status_classified(response, backend="gemini_api")
        body = response.json()

        candidates = body.get("candidates") or []
        if not candidates:
            return _Response(content=[], usage=_Usage())
        parts = candidates[0].get("content", {}).get("parts", [])
        blocks: list[_Block] = []
        call_index = 0
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "functionCall" in part:
                call = part["functionCall"]
                call_index += 1
                provider_call_id = call.get("id")
                block_id = (
                    provider_call_id
                    if isinstance(provider_call_id, str) and provider_call_id
                    else f"call_{call_index}"
                )
                blocks.append(
                    _Block(
                        type="tool_use",
                        id=block_id,
                        name=call["name"],
                        input=dict(call.get("args", {})),
                        gemini_part=copy.deepcopy(part),
                        gemini_call_id=(
                            provider_call_id
                            if isinstance(provider_call_id, str) and provider_call_id
                            else None
                        ),
                    )
                )
            elif "text" in part:
                blocks.append(
                    _Block(type="text", text=part["text"], gemini_part=copy.deepcopy(part))
                )
            else:
                # Preserve unrecognized model Parts too: Gemini 3 can attach
                # signatures to Parts other than function calls.
                blocks.append(_Block(type="gemini_part", gemini_part=copy.deepcopy(part)))

        usage_meta = body.get("usageMetadata", {})
        return _Response(
            content=blocks,
            usage=_Usage(
                input_tokens=usage_meta.get("promptTokenCount", 0),
                output_tokens=usage_meta.get("candidatesTokenCount", 0),
            ),
        )


class GeminiApiClient:
    """`AnthropicClientLike` adapter over Gemini's `generateContent` API.

    Note on key format/transport: since 2026-05-28 Google AI Studio issues
    "auth keys" starting with `AQ.` instead of the legacy `AIzaSy...`
    standard keys, and auth keys are rejected by the `?key=` query-param
    style -- they must go in the `x-goog-api-key` header (which Google's
    docs say works for legacy `AIzaSy` keys too), confirmed via
    https://ai.google.dev/gemini-api/docs/api-key and Google's own AI
    Developer forum threads reporting the exact 401 this project's
    original `?key=` implementation hit. Don't switch back to `?key=`.

    Note on tool_use ids: Gemini's `functionCall`/`functionResponse` protocol
    matches calls by function *name*, not by an id the way Anthropic/OpenAI
    do -- this adapter invents a `call_N` id per response purely so
    `runtime_agent.py`'s existing "match tool_result.tool_use_id to the
    block that requested it" bookkeeping has something to key on; it is
    never sent back to Gemini (only the function *name* is, in
    `functionResponse.name`). Fine for this loop's single-call-then-respond
    pattern; would need revisiting if Gemini ever calls the same tool twice
    in one turn (not observed in this project's own live check call).
    """

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.gemini_api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        self._messages = _GeminiMessages(key)

    @property
    def messages(self) -> _GeminiMessages:
        return self._messages
