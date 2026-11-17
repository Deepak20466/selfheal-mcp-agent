"""Multi-turn, tool-using Claude repair loop for a frozen VIMJ case.

The model starts with only the task, the failing command and its failure text. It must discover
the problem itself with four tools: list_files, read_file, run_tests and propose_patch. Each model
turn is a fresh `claude -p` process (no tools of its own, no session persistence, no MCP, empty
cwd) through the claude.ai subscription login; the tools are implemented here. Tests run through
the real Bubblewrap entry point via the file-queue worker (vimj_sandbox_worker, running as the
benchmark user); nothing from the repository executes in this process. propose_patch may touch
only the case's one allowed source file. The final diff is verified independently by vimj_bench.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from benchmarks.real_bugs import claude_cli_provider as cli
from benchmarks.real_bugs import unresolved_eval as ue
from benchmarks.real_bugs import vimj_bench
from benchmarks.real_bugs.vimj_cases import Case, get_case
from mcp_server.execution_sandbox import _is_secret_file

JOBS_ROOT = Path("/tmp/vimj-stage/jobs")  # noqa: S108 - fixed operator staging area
TREES_ROOT = Path("/tmp/vimj-stage/agent-runs")  # noqa: S108 - fixed operator staging area
MAX_MODEL_REQUESTS = 12
MAX_WALL_SECONDS = 900
FILE_CHARS = 20_000
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".sandbox-home", ".sandbox-tmp"}

SYSTEM = (
    "You are an autonomous software engineer repairing a bug in a FastAPI backend, working "
    "through the supplied tools only. Investigate before editing: read the relevant files, "
    "propose a minimal patch with propose_patch, then confirm with run_tests. You may only "
    "change the one allowed source file, never a test. When the failing test and the rest of the "
    "test suite pass, stop calling tools and give a short final summary."
)
TASK = """Repair task.
The regression test below is failing. Find the cause and fix it.

Failing command (run it with run_tests):
{test_id}

Failure output:
{failure}

Allowed to modify: {source_path} only. Do not modify tests.
Use propose_patch with a unified diff ("--- a/{source_path}" / "+++ b/{source_path}") that is
complete (each proposal replaces your previous one, starting again from the original file).
After a patch is applied, verify with run_tests on the failing test and on the tests/ directory.
"""
TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": "List entries of a directory relative to the backend root.",
        "input_schema": {
            "type": "object",
            "properties": {"directory": {"type": "string"}},
            "required": ["directory"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file relative to the backend root (truncated at 20000 chars).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run pytest in the sandbox on the current tree. target is a tests/ path.",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "propose_patch",
        "description": "Apply a unified diff to the allowed source file; replaces prior ones.",
        "input_schema": {
            "type": "object",
            "properties": {"diff": {"type": "string"}},
            "required": ["diff"],
        },
    },
]


class Workspace:
    def __init__(self, tree: Path, case: Case, baseline_source: str) -> None:
        self.tree = tree
        self.case = case
        self.baseline_source = baseline_source
        self.last_applied_diff: str | None = None

    def _safe(self, relative: str) -> Path | None:
        path = (self.tree / relative).resolve()
        if path != self.tree.resolve() and self.tree.resolve() not in path.parents:
            return None
        if any(part in SKIP_DIRS for part in path.relative_to(self.tree.resolve()).parts):
            return None
        if path.is_file() and _is_secret_file(path.name):
            return None
        return path

    def list_files(self, directory: str) -> str:
        path = self._safe(directory or ".")
        if path is None or not path.is_dir():
            return "error: not a readable directory"
        names = sorted(
            entry.name + ("/" if entry.is_dir() else "")
            for entry in path.iterdir()
            if entry.name not in SKIP_DIRS and not _is_secret_file(entry.name)
        )
        return "\n".join(names)[:FILE_CHARS]

    def read_file(self, relative: str) -> str:
        path = self._safe(relative)
        if path is None or not path.is_file():
            return "error: not a readable file"
        text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        if len(text) > FILE_CHARS:
            return text[:FILE_CHARS] + f"\n[truncated {len(text) - FILE_CHARS} characters]"
        return text

    def run_tests(self, target: str) -> str:
        job = JOBS_ROOT / uuid.uuid4().hex
        job.mkdir(parents=True)
        job.chmod(0o777)
        (job / "request.json").write_text(
            json.dumps({"tree": str(self.tree), "target": target}), encoding="utf-8"
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if (job / "result.json").exists():
                result = json.loads((job / "result.json").read_text(encoding="utf-8"))
                if "error" in result:
                    return f"error: {result['error']}"
                if result.get("sandbox_unavailable"):
                    return "error: sandbox unavailable"
                verdict = "PASSED" if result["passed"] else "FAILED"
                tail = result["output_tail"][-6000:]
                return f"{verdict} (return code {result['return_code']})\n{tail}"
            time.sleep(0.3)
        return "error: test run timed out waiting for the sandbox worker"

    def propose_patch(self, diff: str) -> str:
        diff = diff if diff.endswith("\n") else diff + "\n"
        try:
            paths = ue.extract_paths(diff)
        except Exception as exc:
            return f"rejected: could not parse diff ({type(exc).__name__})"
        if paths != {self.case.source_path}:
            return f"rejected: diff must touch only {self.case.source_path}"
        source = self.tree / self.case.source_path
        source.write_text(self.baseline_source, encoding="utf-8", newline="")
        git = shutil.which("git")
        if git is None:
            return "error: git unavailable"
        flags = ["--recount", "--ignore-whitespace", "--whitespace=nowarn"]
        patch_file = self.tree.parent / "proposal.patch"
        patch_file.write_text(diff, encoding="utf-8")
        check = subprocess.run(  # noqa: S603 - fixed args on the run's own scratch tree
            [git, "apply", "--check", *flags, str(patch_file)],
            cwd=self.tree,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check.returncode != 0:
            return f"rejected: patch does not apply: {check.stderr.strip()[:400]}"
        subprocess.run(  # noqa: S603 - fixed args on the run's own scratch tree
            [git, "apply", *flags, str(patch_file)], cwd=self.tree, check=True, timeout=30
        )
        self.last_applied_diff = diff
        return "applied; now verify with run_tests"


def _call_tool(workspace: Workspace, name: str, arguments: dict[str, Any]) -> str:
    try:
        if name == "list_files":
            return workspace.list_files(str(arguments.get("directory", "")))
        if name == "read_file":
            return workspace.read_file(str(arguments.get("path", "")))
        if name == "run_tests":
            return workspace.run_tests(str(arguments.get("target", "")))
        if name == "propose_patch":
            return workspace.propose_patch(str(arguments.get("diff", "")))
    except Exception as exc:  # a tool failure is reported to the model, not fatal
        return f"error: {type(exc).__name__}: {exc}"[:500]
    return f"error: unknown tool {name}"


async def run_loop(candidate_root: Path, case: Case, model: str, out_dir: Path) -> dict[str, Any]:
    backend = vimj_bench.validate_checkout(candidate_root, case)
    run_root = TREES_ROOT / case.case_id / out_dir.name
    if run_root.exists():
        raise ue.EvaluationSafetyError("run directory already exists; refusing to overwrite")
    run_root.mkdir(parents=True)
    run_root.chmod(0o755)
    tree = vimj_bench.stage_baseline(backend, case, run_root / "tree")
    for path in [run_root, *run_root.rglob("*")]:
        path.chmod(path.stat().st_mode | 0o444 | (0o111 if path.is_dir() else 0))
    baseline_source = (tree / case.source_path).read_text(encoding="utf-8")
    workspace = Workspace(tree, case, baseline_source)

    binary = os.environ.get("CLAUDE_BINARY") or shutil.which("claude")
    if not binary:
        raise cli.ClaudeCLINotFoundError("claude executable not found; set CLAUDE_BINARY")
    provider = cli.ClaudeCLIProvider.from_verified_subscription(binary=binary, model=model)
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": TASK.format(
                test_id=case.test_id, failure=case.failure_text, source_path=case.source_path
            ),
        }
    ]
    transcript: list[dict[str, Any]] = []
    started = time.monotonic()
    finish = "max_requests"
    for request_number in range(1, MAX_MODEL_REQUESTS + 1):
        if time.monotonic() - started > MAX_WALL_SECONDS:
            finish = "wall_clock_limit"
            break
        response = await provider.messages.create(
            system=SYSTEM, messages=messages, tools=TOOLS, request_timeout_seconds=300
        )
        calls = [block for block in response.content if block.type == "tool_use"]
        text = " ".join(block.text or "" for block in response.content if block.type == "text")
        transcript.append(
            {
                "request": request_number,
                "text": text[:1500],
                "tool_calls": [{"name": c.name, "input": _trim(c.input)} for c in calls],
            }
        )
        if not calls:
            finish = "model_finished"
            break
        messages.append({"role": "assistant", "content": list(response.content)})
        results = []
        for block in calls:
            output = _call_tool(workspace, str(block.name), dict(block.input))
            transcript[-1].setdefault("tool_results", []).append(
                {"name": block.name, "result_head": output[:600]}
            )
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - single local mkdir
    patch_text = workspace.last_applied_diff or ""
    (out_dir / "agent.patch").write_text(patch_text, encoding="utf-8")
    result = {
        "case_id": case.case_id,
        "route": "claude.ai subscription via Claude Code CLI (no API key); usage unmetered",
        "model_requested": model,
        "finish_reason": finish,
        "model_requests": len(transcript),
        "tool_call_counts": _counts(transcript),
        "wall_seconds": round(time.monotonic() - started, 1),
        "patch_proposed": bool(patch_text),
        "patch_sha256": ue.sha256_file(out_dir / "agent.patch") if patch_text else None,
        "usage": provider.usage_summary(),
    }
    (out_dir / "transcript.json").write_text(json.dumps(transcript, indent=1), encoding="utf-8")
    (out_dir / "generation.json").write_text(
        json.dumps(result, indent=1, default=str), encoding="utf-8"
    )
    shutil.rmtree(run_root, ignore_errors=True)
    return result


def _trim(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:2000] if isinstance(v, str) else v) for k, v in arguments.items()}


def _counts(transcript: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for turn in transcript:
        for call in turn["tool_calls"]:
            counts[call["name"]] = counts.get(call["name"], 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        raise SystemExit("refusing to overwrite a non-empty output directory")
    result = asyncio.run(
        run_loop(
            args.candidate_root.expanduser().resolve(),
            get_case(args.case),
            args.model,
            args.out_dir,
        )
    )
    print(json.dumps({k: v for k, v in result.items() if k != "usage"}, indent=1))
    return 0 if result["patch_proposed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
