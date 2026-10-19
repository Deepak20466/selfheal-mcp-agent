"""Resolve and allowlist a failed run's target without evaluating target code."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,180}$")


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def resolve(event: dict[str, Any], allowlist: Path, expected_repository: str) -> dict[str, str]:
    allowed = {
        line.strip().lower()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if expected_repository.lower() not in allowed:
        raise ValueError("current repository is not in repair-allowed-repositories.txt")

    repository = event.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("GitHub event is missing repository metadata")
    full_name = _string(repository.get("full_name"))
    if full_name.lower() != expected_repository.lower() or full_name.lower() not in allowed:
        raise ValueError("event repository is not allowlisted")
    if repository.get("private") is not False:
        raise ValueError("external inference is restricted to explicitly public repositories")

    run = event.get("workflow_run")
    if not isinstance(run, dict):
        raise ValueError("only trusted workflow_run CI failures can trigger repairs")
    if run.get("name") != "CI" or run.get("conclusion") != "failure":
        raise ValueError("repair triggers only for failed CI workflow runs")
    head_repo = run.get("head_repository")
    if (
        not isinstance(head_repo, dict)
        or _string(head_repo.get("full_name")).lower() != full_name.lower()
    ):
        raise ValueError("fork-originated CI runs are not eligible for repair")

    head_sha = _string(run.get("head_sha"))
    head_branch = _string(run.get("head_branch"))
    if head_branch.startswith("selfheal/repair-"):
        raise ValueError("repairs are not recursively triggered by repair branches")
    pull_requests = run.get("pull_requests")
    base_branch = ""
    if isinstance(pull_requests, list) and pull_requests:
        pull = pull_requests[0]
        if isinstance(pull, dict):
            head = pull.get("head")
            if isinstance(head, dict) and _SHA.fullmatch(_string(head.get("sha"))):
                head_sha = _string(head.get("sha"))
            base = pull.get("base")
            if isinstance(base, dict):
                base_branch = _string(base.get("ref"))
    if not base_branch:
        base_branch = _string(repository.get("default_branch"))
    if not _SHA.fullmatch(head_sha):
        raise ValueError("workflow run did not provide a full commit SHA")
    if (
        not _BRANCH.fullmatch(base_branch)
        or ".." in base_branch
        or "//" in base_branch
        or base_branch.endswith("/")
        or "@{" in base_branch
    ):
        raise ValueError("workflow run base branch has an unsafe name")
    run_id = str(run.get("id", ""))
    if not run_id.isdigit():
        raise ValueError("workflow run ID is invalid")
    return {
        "repository": full_name,
        "sha": head_sha,
        "base": base_branch,
        "run_id": run_id,
    }


def main() -> int:
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    expected_repository = os.environ["GITHUB_REPOSITORY"]
    enabled = os.environ.get("SELFHEAL_REPAIR_ENABLED", "").lower() == "true"
    if not enabled:
        print("repair workflow disabled; set repository variable SELFHEAL_REPAIR_ENABLED=true")
        return 1
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
        if not isinstance(event, dict):
            raise ValueError("GitHub event must be an object")
        result = resolve(
            event,
            Path("repair-allowed-repositories.txt"),
            expected_repository,
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"repair target rejected: {exc}", file=sys.stderr)
        return 1
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
    print(f"allowlisted target resolved: {result['repository']} at {result['sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
