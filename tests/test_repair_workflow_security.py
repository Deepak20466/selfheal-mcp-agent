"""Trust boundaries for the secret-split OpenRouter workflow."""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.resolve_repair_target import resolve
from scripts.test_repair_patch import _unittest_count

ROOT = Path(__file__).resolve().parents[1]


def test_openrouter_key_is_scoped_to_inference_steps_only() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "repair-pr.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert jobs["infer-initial"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["infer-retry"]["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["test-initial"]["permissions"] == {"contents": "read"}
    assert jobs["test-retry"]["permissions"] == {"contents": "read"}
    assert jobs["create-repair-pr"]["permissions"] == {
        "actions": "read",
        "contents": "write",
        "pull-requests": "write",
    }
    for name, job in jobs.items():
        secret_steps = [
            step
            for step in job.get("steps", [])
            if "OPENROUTER_API_KEY" in str(step.get("env", {}))
        ]
        assert len(secret_steps) <= (1 if name in {"infer-initial", "infer-retry"} else 0), name


def test_test_jobs_are_offline_and_no_provider_or_write_secret_is_present() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "repair-pr.yml").read_text())
    for name in ("test-initial", "test-retry"):
        job = workflow["jobs"][name]
        serialized = str(job)
        assert "secrets." not in serialized
        assert "OPENROUTER_API_KEY" not in serialized
        assert "persist-credentials'" in serialized or "persist-credentials" in serialized
        assert "verify_repair_sandbox.py" in serialized
        assert "test_repair_patch.py" in serialized
        assert (
            "read_only_workspace_paths" in (ROOT / "scripts" / "test_repair_patch.py").read_text()
        )
    assert "GITHUB_TOKEN" not in str(workflow["jobs"]["test-initial"].get("env", {}))


def test_pr_job_is_downstream_of_successful_isolated_regression_and_never_merges() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "repair-pr.yml").read_text())
    events = workflow.get("on", workflow.get(True, {}))
    assert set(events) == {"workflow_run"}
    assert events["workflow_run"]["workflows"] == ["CI"]
    create = workflow["jobs"]["create-repair-pr"]
    assert {"test-initial", "test-retry", "infer-initial", "infer-retry", "prepare"} == set(
        create["needs"]
    )
    assert "gh pr merge" not in (ROOT / ".github" / "workflows" / "repair-pr.yml").read_text()
    assert "selfheal/repair-" in (ROOT / ".github" / "workflows" / "repair-pr.yml").read_text()


def test_prepare_rejects_fork_ci_and_nonfailed_or_non_ci_runs(tmp_path: Path) -> None:
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("example/app\n", encoding="utf-8")
    event = {
        "repository": {"full_name": "example/app", "default_branch": "main", "private": False},
        "workflow_run": {
            "id": 101,
            "name": "CI",
            "conclusion": "failure",
            "head_sha": "a" * 40,
            "head_repository": {"full_name": "example/app"},
            "pull_requests": [],
        },
    }
    assert resolve(event, allowlist, "example/app")["base"] == "main"

    fork_event = {
        **event,
        "workflow_run": {**event["workflow_run"], "head_repository": {"full_name": "evil/app"}},
    }
    try:
        resolve(fork_event, allowlist, "example/app")
    except ValueError as exc:
        assert "fork-originated" in str(exc)
    else:
        raise AssertionError("fork-triggered repair must be denied")

    success_event = {**event, "workflow_run": {**event["workflow_run"], "conclusion": "success"}}
    try:
        resolve(success_event, allowlist, "example/app")
    except ValueError as exc:
        assert "failed CI" in str(exc)
    else:
        raise AssertionError("successful run must not start repair")


def test_repair_requires_a_completed_nonempty_unittest_run() -> None:
    assert _unittest_count("Ran 0 tests in 0.000s\nOK\n") == 0
    assert _unittest_count("Ran 1 test in 0.011s\nOK\n") == 1
    assert _unittest_count("test_fake (x) ... ok\n") is None
