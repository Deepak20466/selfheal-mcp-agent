"""Regression checks for credential boundaries in GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _workflow(name: str) -> dict[str, object]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_checkout_never_persists_github_token_in_git_config() -> None:
    checkouts: list[tuple[str, dict[str, object]]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in workflow.get("jobs", {}).items():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    checkouts.append((f"{path.name}:{job_name}", step))

    assert checkouts, "expected at least one workflow checkout"
    for location, step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False, location


def test_release_write_permission_isolated_from_build_job() -> None:
    workflow = _workflow("release.yml")
    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish-to-pypi"]["permissions"] == {"contents": "read"}

    release = jobs["publish-release"]
    assert release["permissions"] == {"contents": "write"}
    assert all(
        not str(step.get("uses", "")).startswith("actions/checkout@") and "run" not in step
        for step in release["steps"]
    )
    assert any(
        step.get("uses", "").startswith("softprops/action-gh-release@") for step in release["steps"]
    )


def test_pull_request_workflows_do_not_reference_repository_secrets() -> None:
    secret_reference = re.compile(r"\$\{\{\s*secrets\.", re.IGNORECASE)
    workflows = sorted(WORKFLOWS.glob("*.yml"))

    for path in workflows:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        events = workflow.get("on", workflow.get(True, {}))
        if isinstance(events, dict) and ({"pull_request", "pull_request_target"} & events.keys()):
            assert not secret_reference.search(path.read_text(encoding="utf-8")), path.name


def test_privileged_ci_notifier_checks_out_only_trusted_default_branch() -> None:
    workflow = _workflow("ci-failure.yml")
    events = workflow.get("on", workflow.get(True, {}))
    workflow_run = events["workflow_run"]
    assert workflow_run["types"] == ["in_progress", "completed"]

    job = workflow["jobs"]["notify-ci-run"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in job
    checkouts = [
        step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["ref"] == "main"
    assert checkouts[0]["with"]["persist-credentials"] is False
    assert not any("download-artifact" in str(step.get("uses", "")) for step in job["steps"])
