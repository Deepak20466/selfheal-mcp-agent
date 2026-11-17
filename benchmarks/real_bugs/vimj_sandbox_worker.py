"""File-queue worker that runs frozen-case pytest targets through the real Bubblewrap entry point.

Runs as the `benchmark` user (the only account under the 64-process cap). It never executes
anything natively: each job is one `python -m pytest -q <target>` through run_untrusted_command,
with the same read-only mounts the verifier uses. Jobs are directories under JOBS_ROOT holding
request.json {"tree": <dir under TREES_ROOT>, "target": <tests/... node id>}; the worker writes
result.json next to it. Exits when JOBS_ROOT/STOP exists or after an idle timeout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from benchmarks.real_bugs import unresolved_eval as ue
from mcp_server.execution_sandbox import run_untrusted_command

JOBS_ROOT = Path("/tmp/vimj-stage/jobs")  # noqa: S108 - fixed operator staging area
TREES_ROOT = Path("/tmp/vimj-stage/agent-runs")  # noqa: S108 - fixed operator staging area
TARGET_RE = re.compile(r"^tests(/[\w.\-]+)*\.py(::[\w]+(::[\w]+)?)?$|^tests/?$")


def run_job(job: Path, python: Path) -> dict[str, object]:
    request = json.loads((job / "request.json").read_text(encoding="utf-8"))
    tree = Path(request["tree"]).resolve()
    target = str(request["target"])
    if TREES_ROOT.resolve() not in tree.parents or not tree.is_dir():
        return {"error": "tree outside the allowed root"}
    if not TARGET_RE.match(target) or ".." in target.split("/"):
        return {"error": "target is not a tests/ path or node id"}
    outcome = asyncio.run(
        run_untrusted_command(
            [str(python), "-m", "pytest", "-q", target],
            cwd=tree,
            timeout=120,
            read_only_paths=[python.parents[1], Path(sys.base_prefix)],
        )
    )
    return {
        "passed": bool(outcome.get("passed")),
        "return_code": outcome.get("return_code"),
        "sandbox": outcome.get("sandbox"),
        "sandbox_unavailable": bool(outcome.get("sandbox_unavailable")),
        "output_tail": str(outcome.get("output", ""))[-8000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idle-seconds", type=float, default=1800)
    args = parser.parse_args()
    python = ue.VENV_ROOT / ue.CASE_ID / "bin" / "python"
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    last_activity = time.monotonic()
    print("worker ready", flush=True)
    while not (JOBS_ROOT / "STOP").exists():
        if time.monotonic() - last_activity > args.idle_seconds:
            print("idle timeout", flush=True)
            break
        pending = [
            job
            for job in sorted(JOBS_ROOT.iterdir())
            if job.is_dir()
            and (job / "request.json").exists()
            and not (job / "result.json").exists()
        ]
        for job in pending:
            try:
                result = run_job(job, python)
            except Exception as exc:  # report, never crash the worker
                result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            tmp = job / "result.json.tmp"
            tmp.write_text(json.dumps(result), encoding="utf-8")
            tmp.chmod(0o644)
            tmp.rename(job / "result.json")
            last_activity = time.monotonic()
        time.sleep(0.3)
    print("worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
