"""The runtime_error / contract_violation agentic fix loop (SPEC.md healer-pod).

`run_heal_job` is the whole loop: read the error/violation and code, find the
root cause, write a fix plus a regression test, prove it (fail before the
fix, pass after), retry up to 3 iterations, then either open a PR or — on a
circuit-breaker trip, budget pause, or exhausted low-confidence attempt — stop
without one. Every guardrail SPEC.md lists (write-scope sandbox, patch
size/anti-cheat, budget caps, circuit breakers) is enforced by the modules
this calls (`mcp_server.patch_guard`, `healer.budget`, `healer.
circuit_breaker`), never only by the prompt in `healer/prompts.py` — so an
injection payload embedded in a captured error can talk the model into
*trying* something forbidden, but the surrounding system still refuses it.

Two guardrails worth calling out here specifically: `propose_patch`/
`run_tests` calls always get their `heal_job_id`/`worktree` arguments
overridden from server-side state (never trusted from the model's tool-call
input) — otherwise a compromised or confused model could name a *different*
heal_job_id to borrow that job's (possibly wider) write scope. And a fix
attempt only counts as proven when `run_tests` shows an earlier failure
(the new regression test reproducing the bug) followed by a later pass —
not just a single passing run, which wouldn't demonstrate the test actually
catches the bug.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from core.config import settings
from core.db import session_scope
from core.models import AuditLog, BudgetCategory, FixAttempt, HealJob, HealJobStatus, HealJobType
from healer.anthropic_client import AnthropicClientLike
from healer.budget import is_budget_paused, record_spend
from healer.circuit_breaker import fingerprint_circuit_open
from healer.costs import TokenUsage, compute_cost_usd
from healer.full_suite import combine_evidence, run_full_suite
from healer.github_ops import FixEvidence, open_fix_pull_request, open_low_confidence_issue
from healer.mcp_client import MCPToolClient, MCPToolError
from healer.prompts import SYSTEM_PROMPT, build_initial_messages
from healer.worktree import (
    branch_name_for,
    commit_and_push,
    create_worktree,
    remove_worktree,
    reset_worktree,
)
from mcp_server.github_client import GitHubClient

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3
MAX_TOOL_CALLS_PER_ATTEMPT = 20
MAX_NO_PROGRESS_TOOL_TURNS = 2
MAX_INSPECTION_ONLY_TOOL_TURNS = 4
CLAUDE_MAX_TOKENS = 8000

RUNTIME_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_code",
        "list_files",
        "get_git_blame",
        "get_recent_commits",
        "run_tests",
        "propose_patch",
    }
)


@dataclass(frozen=True)
class AttemptResult:
    success: bool
    root_cause: str
    diff_stat: str
    test_output: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: Decimal


async def _audit(
    session: Any, *, action: str, heal_job_id: int | None, details: dict[str, Any] | None = None
) -> None:
    session.add(AuditLog(action=action, actor="healer", details=details, heal_job_id=heal_job_id))
    await session.flush()


async def run_heal_job(
    job_id: int,
    *,
    anthropic_client: AnthropicClientLike,
    mcp: MCPToolClient,
    github: GitHubClient,
    remote: str = "origin",
) -> None:
    """Run the full fix loop for one `runtime_error`/`contract_violation` heal_job.

    `remote` defaults to `origin` (production); tests override it to a
    throwaway local bare repo so a test run never pushes to the real GitHub
    repo (see `tests/conftest.py`'s `fake_git_remote` fixture).
    """
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        if job is None:
            logger.warning("runtime_agent.job_not_found", heal_job_id=job_id)
            return
        fingerprint = job.fingerprint
        job_type = job.type
        source_error_id = job.source_error_id
        source_contract_violation_id = job.source_contract_violation_id

        if await fingerprint_circuit_open(
            session, fingerprint, max_attempts=settings.max_heal_attempts_per_fingerprint_24h
        ):
            job.status = HealJobStatus.FAILED
            job.error_message = (
                "circuit breaker: too many heal attempts for this fingerprint in 24h"
            )
            job.finished_at = datetime.now(UTC)
            await _audit(
                session,
                action="circuit_breaker_tripped",
                heal_job_id=job_id,
                details={"fingerprint": fingerprint},
            )
            return

    if job_type == HealJobType.RUNTIME_ERROR:
        source_kind = "error"
        source = await mcp.call_tool("get_error", {"error_id": source_error_id})
        error_summary = (
            f"{source['exception_type']} in {source['file_path']}:{source['line_number']}"
        )
    elif job_type == HealJobType.CONTRACT_VIOLATION:
        source_kind = "contract_violation"
        source = await mcp.call_tool(
            "get_contract_violation", {"violation_id": source_contract_violation_id}
        )
        error_summary = (
            f"contract violation at {source['endpoint']} "
            f"({source['file_path']}:{source['line_number']})"
        )
    else:
        raise ValueError(
            f"run_heal_job only handles runtime_error/contract_violation, got {job_type}"
        )

    tool_schemas = [
        schema for schema in await mcp.list_tool_schemas() if schema["name"] in RUNTIME_TOOL_NAMES
    ]

    worktree_name = f"heal-{job_id}"
    branch = branch_name_for(fingerprint, job_id)
    worktree_path = await create_worktree(worktree_name, branch, remote=remote)

    attempts_summaries: list[str] = []
    previous_summary: str | None = None
    job_total_tokens = 0

    try:
        for attempt_number in range(1, MAX_ATTEMPTS + 1):
            async with session_scope() as session:
                if await is_budget_paused(
                    session,
                    category=BudgetCategory.HEALER,
                    daily_budget_usd=settings.daily_budget_usd,
                ):
                    job = await session.get(HealJob, job_id)
                    assert job is not None
                    job.status = HealJobStatus.PAUSED_BUDGET
                    await _audit(
                        session,
                        action="budget_paused",
                        heal_job_id=job_id,
                        details={"category": "healer"},
                    )
                    return

            if job_total_tokens > settings.max_tokens_per_job:
                await _mark_failed_and_open_issue(
                    github,
                    job_id=job_id,
                    fingerprint=fingerprint,
                    error_summary=error_summary,
                    reason="exceeded MAX_TOKENS_PER_JOB for this heal_job",
                    attempts_summary="\n\n".join(attempts_summaries),
                )
                return

            messages = build_initial_messages(
                source_kind=source_kind,
                source=source,
                attempt_number=attempt_number,
                max_attempts=MAX_ATTEMPTS,
                heal_job_id=job_id,
                worktree=worktree_name,
                previous_attempts_summary=previous_summary,
            )
            result = await _run_one_attempt(
                anthropic_client=anthropic_client,
                mcp=mcp,
                job_id=job_id,
                worktree_name=worktree_name,
                messages=messages,
                tool_schemas=tool_schemas,
            )
            job_total_tokens += result.input_tokens + result.output_tokens

            async with session_scope() as session:
                job = await session.get(HealJob, job_id)
                assert job is not None
                job.attempt_count = attempt_number
                session.add(
                    FixAttempt(
                        heal_job_id=job_id,
                        attempt_number=attempt_number,
                        root_cause=result.root_cause,
                        diff=None,
                        test_output=result.test_output,
                        passed=result.success,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        cached_tokens=result.cached_tokens,
                        cost_usd=result.cost_usd,
                    )
                )
                await record_spend(
                    session, category=BudgetCategory.HEALER, cost_usd=result.cost_usd
                )

            summary = (
                f"Attempt {attempt_number}: {'succeeded' if result.success else 'failed'}. "
                f"{result.root_cause}\nTest output tail:\n{result.test_output[-1000:]}"
            )
            attempts_summaries.append(summary)
            previous_summary = summary

            if result.success:
                commit_msg = (
                    f"Auto-fix: {error_summary}\n\nheal_job #{job_id}, fingerprint {fingerprint}"
                )
                await commit_and_push(
                    worktree_path,
                    branch,
                    message=commit_msg,
                    remote=remote,
                    github_token=await github.get_access_token(),
                )
                evidence = FixEvidence(
                    heal_job_id=job_id,
                    fingerprint=fingerprint,
                    root_cause=result.root_cause,
                    diff_stat=result.diff_stat,
                    test_output=result.test_output,
                    error_summary=error_summary,
                )
                pr = await open_fix_pull_request(
                    github, branch=branch, base="main", evidence=evidence
                )

                async with session_scope() as session:
                    job = await session.get(HealJob, job_id)
                    assert job is not None
                    job.status = HealJobStatus.PR_OPENED
                    job.pr_opened_at = datetime.now(UTC)
                    job.branch_name = branch
                    job.pr_number = pr["number"]
                    await _audit(
                        session,
                        action="pr_opened",
                        heal_job_id=job_id,
                        details={"pr_number": pr["number"], "branch": branch},
                    )
                return

            if attempt_number < MAX_ATTEMPTS:
                await reset_worktree(worktree_path)

        await _mark_failed_and_open_issue(
            github,
            job_id=job_id,
            fingerprint=fingerprint,
            error_summary=error_summary,
            reason="exhausted all fix attempts without a verified passing regression test",
            attempts_summary="\n\n".join(attempts_summaries),
        )
    finally:
        await remove_worktree(worktree_name, branch)


async def _mark_failed_and_open_issue(
    github: GitHubClient,
    *,
    job_id: int,
    fingerprint: str,
    error_summary: str,
    reason: str,
    attempts_summary: str,
) -> None:
    evidence = FixEvidence(
        heal_job_id=job_id,
        fingerprint=fingerprint,
        root_cause=reason,
        diff_stat="",
        test_output=attempts_summary,
        error_summary=error_summary,
    )
    issue = await open_low_confidence_issue(
        github, evidence=evidence, attempts_summary=attempts_summary
    )

    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        assert job is not None
        job.status = HealJobStatus.FAILED
        job.error_message = reason
        job.finished_at = datetime.now(UTC)
        await _audit(
            session,
            action="heal_failed",
            heal_job_id=job_id,
            details={"reason": reason, "issue_number": issue.get("number")},
        )


async def _run_one_attempt(
    *,
    anthropic_client: AnthropicClientLike,
    mcp: MCPToolClient,
    job_id: int,
    worktree_name: str,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    system_prompt: str = SYSTEM_PROMPT,
    max_total_tokens: int | None = None,
    model_name: str | None = None,
) -> AttemptResult:
    """One iteration: converse with Claude, executing tool calls, until it stops."""
    total_input = total_output = total_cached = 0
    total_cost = Decimal("0")
    test_results: list[bool] = []
    last_diff_stat = ""
    last_test_output = ""
    final_text = ""
    failed_tool_calls: dict[str, int] = {}
    successful_read_calls: set[str] = set()
    no_progress_turns = 0
    inspection_only_turns = 0

    for _ in range(MAX_TOOL_CALLS_PER_ATTEMPT):
        token_count = total_input + total_output
        if max_total_tokens is not None and token_count >= max_total_tokens:
            break
        max_response_tokens = CLAUDE_MAX_TOKENS
        if max_total_tokens is not None:
            max_response_tokens = min(max_response_tokens, max_total_tokens - token_count)
        response = await anthropic_client.messages.create(
            model=model_name or settings.anthropic_model,
            max_tokens=max_response_tokens,
            thinking={"type": "disabled"},
            system=[
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ],
            tools=tool_schemas,
            messages=messages,
        )

        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        total_input += input_tokens
        total_output += output_tokens
        total_cached += cache_read
        total_cost += compute_cost_usd(
            settings.anthropic_model,
            TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_write,
                cache_read_input_tokens=cache_read,
            ),
        )

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        text_blocks = [block.text for block in response.content if block.type == "text"]
        if text_blocks:
            final_text = " ".join(text_blocks)

        if not tool_use_blocks:
            break

        messages.append({"role": "assistant", "content": response.content})

        begin_model_turn = getattr(mcp, "begin_model_turn", None)
        if callable(begin_model_turn):
            begin_model_turn()

        tool_results: list[dict[str, Any]] = []
        turn_made_progress = False
        repeated_failed_call = False
        turn_attempted_repair_action = False
        for block in tool_use_blocks:
            # Never trust the model's own heal_job_id/worktree — always the
            # server-side values for *this* job, so the write scope
            # propose_patch derives from heal_job_id can't be widened by a
            # confused or injected tool call naming a different job.
            valid_arguments = isinstance(block.input, dict)
            arguments = dict(block.input) if valid_arguments else {}
            if block.name in {"run_tests", "propose_patch"}:
                turn_attempted_repair_action = True
            if block.name == "read_file" and valid_arguments:
                line_range = arguments.get("start_line")
                if (
                    arguments.get("end_line") is None
                    and isinstance(line_range, list)
                    and len(line_range) == 2
                    and all(
                        isinstance(line, int) and not isinstance(line, bool) and line >= 1
                        for line in line_range
                    )
                    and line_range[0] <= line_range[1]
                ):
                    # Some OpenAI-compatible models encode the requested range
                    # as a pair in start_line despite the scalar MCP schema.
                    arguments["start_line"], arguments["end_line"] = line_range
            if block.name in ("propose_patch", "run_tests"):
                arguments["worktree"] = worktree_name
            if block.name == "propose_patch":
                arguments["heal_job_id"] = job_id

            call_signature = json.dumps(
                [block.name, arguments], sort_keys=True, separators=(",", ":"), default=str
            )
            is_error = False
            try:
                if not valid_arguments:
                    raise MCPToolError(
                        "Tool arguments must be a JSON object; retry with an object."
                    )
                result = await mcp.call_tool(block.name, arguments)
            except MCPToolError as exc:
                result = str(exc)
                is_error = True
                failed_tool_calls[call_signature] = failed_tool_calls.get(call_signature, 0) + 1
                repeated_failed_call = (
                    repeated_failed_call or failed_tool_calls[call_signature] >= 2
                )

            if not is_error and block.name == "run_tests" and isinstance(result, dict):
                test_results.append(bool(result.get("passed")))
                last_test_output = str(result.get("output", ""))[-4000:]
            if not is_error and block.name == "propose_patch" and isinstance(result, dict):
                last_diff_stat = str(result.get("diff_stat", ""))
            if not is_error:
                if block.name in {"read_file", "list_files"}:
                    if call_signature not in successful_read_calls:
                        successful_read_calls.add(call_signature)
                        turn_made_progress = True
                else:
                    # Patch and test operations are meaningful even when the
                    # model repeats a test path to establish before/after evidence.
                    turn_made_progress = True

            content = result if isinstance(result, str) else json.dumps(result, default=str)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results})
        no_progress_turns = 0 if turn_made_progress else no_progress_turns + 1
        inspection_only_turns = 0 if turn_attempted_repair_action else inspection_only_turns + 1
        if (
            repeated_failed_call
            or no_progress_turns >= MAX_NO_PROGRESS_TOOL_TURNS
            or inspection_only_turns >= MAX_INSPECTION_ONLY_TOOL_TURNS
        ):
            reason = (
                "repeated identical failed tool call"
                if repeated_failed_call
                else "inspection-only tool-turn limit reached"
                if inspection_only_turns >= MAX_INSPECTION_ONLY_TOOL_TURNS
                else "consecutive tool turns made no new progress"
            )
            logger.warning(
                "runtime_agent.attempt_stalled",
                job_id=job_id,
                reason=reason,
                no_progress_turns=no_progress_turns,
            )
            final_text = f"Stopped after {reason}; existing tool and test evidence is preserved."
            break

    # Proven only if an earlier run_tests failed (the new test reproduces the
    # bug) and the last run_tests passed — a single passing run never proves
    # the regression test actually catches anything.
    success = (
        len(test_results) >= 2
        and any(not passed for passed in test_results[:-1])
        and test_results[-1]
    )
    if success:
        suite_passed, suite_output = await run_full_suite(
            mcp, worktree_name=worktree_name, heal_job_id=job_id
        )
        last_test_output = combine_evidence(
            last_test_output, suite_passed=suite_passed, suite_output=suite_output
        )
        success = suite_passed

    return AttemptResult(
        success=success,
        root_cause=final_text or "(agent produced no final summary)",
        diff_stat=last_diff_stat,
        test_output=last_test_output,
        input_tokens=total_input,
        output_tokens=total_output,
        cached_tokens=total_cached,
        cost_usd=total_cost,
    )
