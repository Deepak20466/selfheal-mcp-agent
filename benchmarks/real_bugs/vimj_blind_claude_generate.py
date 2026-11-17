"""Blind, single-shot Claude repair generation for the frozen VIMJ fee-balance case.

The model runs in a FRESH `claude` process with no tools, no session persistence, no MCP, no
repository access and an empty working directory. It sees only the frozen model-visible inputs:
the regression test file, the one allowed source file, the failing command and the baseline
failure output. It is never shown the scripted fixture, the earlier AI patch, or any hint about
the fix. Authentication is the operator's claude.ai subscription login; no API key is used or
forwarded. Subscription usage is unmetered and not verified here.

This module only generates and saves the patch. Verification happens separately inside
Bubblewrap (vimj_ai_repair_check.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.real_bugs import claude_cli_provider as cli
from benchmarks.real_bugs import unresolved_eval as ue
from benchmarks.real_bugs import vimj_bench
from benchmarks.real_bugs.vimj_cases import FEE_BALANCE, Case, get_case

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string"},
        "patch": {"type": "string"},
    },
    "required": ["diagnosis", "patch"],
    "additionalProperties": False,
}
SYSTEM_PROMPT = (
    "You are a careful software engineer repairing a bug in a FastAPI backend. You have no tools "
    "and cannot run code. Reply only with JSON matching the supplied schema: a short diagnosis "
    "and a unified diff."
)
INSTRUCTIONS = """Task: make the failing regression test below pass by fixing the bug in
{source_path}.

Rules:
- Modify only {source_path}. Do not change any test.
- Keep all other behaviour unchanged. Make the smallest correct change.
- Return the fix as a unified diff in the "patch" field, using the paths
  "--- a/{source_path}" and "+++ b/{source_path}", with enough unchanged context
  lines for `git apply` to locate each hunk exactly. Copy context lines exactly as shown.
- Put your root-cause analysis in the "diagnosis" field.

Failing command:
python -m pytest -q {test_id}

Failure output (baseline):
{failure}

=== {test_file} ===
{test_source}

=== {source_path} ===
{source}
"""


def _staged_test_source(backend: Path, case: Case) -> str:
    """Baseline test file with the frozen overlay applied, in a scratch copy, hash-checked."""
    with tempfile.TemporaryDirectory(prefix="blind-stage-") as scratch:
        staged = vimj_bench.stage_baseline(backend, case, Path(scratch) / "repo")
        test_path = staged / case.test_file
        if ue.sha256_text_file(test_path) != case.test_canonical_sha256:
            raise ue.EvaluationSafetyError("staged frozen test hash mismatch")
        return test_path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _extract_json(stdout: str) -> dict[str, Any]:
    envelope = json.loads(stdout)
    if isinstance(envelope, list):
        envelope = next((item for item in reversed(envelope) if "result" in item), envelope[-1])
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = envelope.get("result")
    if isinstance(result, str):
        match = re.search(r"\{.*\}", result, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    raise cli.ClaudeCLIMalformedOutputError("no structured JSON found in CLI output")


def generate(
    candidate_root: Path, model: str, out_dir: Path, case: Case = FEE_BALANCE
) -> dict[str, Any]:
    backend = vimj_bench.validate_checkout(candidate_root.expanduser().resolve(), case)
    source = (backend / case.source_path).read_text(encoding="utf-8").replace("\r\n", "\n")
    prompt = INSTRUCTIONS.format(
        source_path=case.source_path,
        test_file=case.test_file,
        test_id=case.test_id,
        failure=case.failure_text,
        test_source=_staged_test_source(backend, case),
        source=source,
    )
    if len(prompt) > cli.MAX_PROMPT_CHARS:
        raise ue.EvaluationSafetyError("prompt exceeds the adapter size limit")

    binary = os.environ.get("CLAUDE_BINARY") or shutil.which("claude")
    if not binary:
        raise cli.ClaudeCLINotFoundError("claude executable not found; set CLAUDE_BINARY")
    env = cli.build_cli_env(os.environ)
    auth = cli.verify_subscription_login(cli.read_auth_status(binary, env))

    work = Path(tempfile.mkdtemp(prefix="blind-claude-"))
    try:
        mcp_config = work / "empty-mcp.json"
        mcp_config.write_text('{"mcpServers":{}}', encoding="utf-8")
        argv = cli.build_cli_argv(binary=binary, model=model, schema=SCHEMA, mcp_config=mcp_config)
        argv[argv.index("--system-prompt") + 1] = SYSTEM_PROMPT
        started = datetime.now(UTC).isoformat()
        stdout, stderr, code = asyncio.run(
            cli._run_cli(argv, prompt, cwd=work, env=env, limit_seconds=cli.CLI_TIMEOUT_SECONDS)
        )
        finished = datetime.now(UTC).isoformat()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    from sentinel.scrubber import scrub_text

    (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (out_dir / "cli_stdout.json").write_text(scrub_text(stdout), encoding="utf-8")
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "route": "claude.ai subscription via Claude Code CLI (no API key); usage unmetered",
        "model_requested": model,
        "auth": {k: auth.get(k) for k in ("auth_method", "logged_in") if k in auth},
        "cli_calls": 1,
        "started_at_utc": started,
        "finished_at_utc": finished,
        "return_code": code,
        "stderr_tail": scrub_text(stderr[-500:]),
        "prompt_sha256": ue.sha256_file(out_dir / "prompt.txt"),
        "prompt_chars": len(prompt),
    }
    if code != 0:
        record["error"] = "non-zero CLI exit"
        (out_dir / "generation.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record
    reply = _extract_json(stdout)
    patch = str(reply.get("patch", ""))
    if patch and not patch.endswith("\n"):
        patch += "\n"
    (out_dir / "blind.patch").write_text(patch, encoding="utf-8")
    (out_dir / "diagnosis.txt").write_text(str(reply.get("diagnosis", "")), encoding="utf-8")
    record["patch_sha256"] = ue.sha256_file(out_dir / "blind.patch")
    record["patch_paths"] = sorted(ue.extract_paths(patch)) if patch else []
    (out_dir / "generation.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--case", default="fee-balance")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        raise SystemExit("refusing to overwrite a non-empty output directory")
    record = generate(args.candidate_root, args.model, args.out_dir, get_case(args.case))
    print(json.dumps(record, indent=2))
    return 0 if record.get("return_code") == 0 and record.get("patch_sha256") else 1


if __name__ == "__main__":
    raise SystemExit(main())
