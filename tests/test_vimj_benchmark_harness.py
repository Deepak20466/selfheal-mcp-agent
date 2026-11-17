"""Offline tests for the multi-case VIMJ benchmark harness (no sandbox, model or network)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.real_bugs import vimj_agentic_claude_repair as agent
from benchmarks.real_bugs import vimj_blind_claude_generate as blind
from benchmarks.real_bugs import vimj_sandbox_worker as worker
from benchmarks.real_bugs.vimj_cases import CASES, FEE_BALANCE, PASSWORD_RESET, get_case

FIXED_FILE = """import os


def total(a, b):
    return a + b
"""
GOOD_DIFF = """--- a/app/mod.py
+++ b/app/mod.py
@@ -1,5 +1,5 @@
 import os


 def total(a, b):
-    return a + b
+    return a + b + 0
"""


@pytest.fixture
def workspace(tmp_path: Path) -> agent.Workspace:
    tree = tmp_path / "tree"
    (tree / "app").mkdir(parents=True)
    (tree / "app" / "mod.py").write_text(FIXED_FILE, encoding="utf-8")
    (tree / ".env").write_text("SECRET=1", encoding="utf-8")
    (tree / "tests").mkdir()
    case = FEE_BALANCE.__class__(**{**FEE_BALANCE.__dict__, "source_path": "app/mod.py"})
    return agent.Workspace(tree, case, FIXED_FILE)


def test_case_registry_and_overlay_hashes() -> None:
    assert set(CASES) == {"fee-balance", "password-reset-expiry"}
    for case in CASES.values():
        digest = hashlib.sha256(case.overlay_path.read_bytes()).hexdigest()
        assert digest == case.overlay_sha256
        assert len(case.source_sha256) == 64 and len(case.test_canonical_sha256) == 64
        assert "PIN_AFTER" not in case.failure_text
    assert PASSWORD_RESET.reference_patch is not None and PASSWORD_RESET.reference_patch.is_file()
    with pytest.raises(Exception, match="unknown case"):
        get_case("nope")


def test_blind_prompt_template_has_no_fix_hints() -> None:
    for case in CASES.values():
        prompt = blind.INSTRUCTIONS.format(
            source_path=case.source_path,
            test_file=case.test_file,
            test_id=case.test_id,
            failure=case.failure_text,
            test_source="TEST BODY",
            source="SOURCE BODY",
        )
        for hint in ("previous_total", "tzinfo", "replace(tzinfo", "scripted", "reference", "fix:"):
            assert hint not in prompt
        assert case.source_path in prompt and case.test_id in prompt


def test_workspace_blocks_traversal_and_secrets(workspace: agent.Workspace) -> None:
    assert "mod.py" in workspace.list_files("app")
    assert ".env" not in workspace.list_files("")
    assert workspace.read_file("../etc/passwd").startswith("error")
    assert workspace.read_file(".env").startswith("error")
    assert workspace.read_file("app/mod.py").startswith("import os")


def test_propose_patch_allowlist_and_replacement(workspace: agent.Workspace) -> None:
    other = GOOD_DIFF.replace("app/mod.py", "app/other.py")
    assert workspace.propose_patch(other).startswith("rejected")
    assert workspace.propose_patch(GOOD_DIFF).startswith("applied")
    assert "a + b + 0" in (workspace.tree / "app" / "mod.py").read_text(encoding="utf-8")
    # A later proposal replaces the earlier one, starting again from the original file.
    assert workspace.propose_patch(GOOD_DIFF).startswith("applied")
    assert (workspace.tree / "app" / "mod.py").read_text(encoding="utf-8").count("+ 0") == 1
    assert workspace.last_applied_diff is not None


def test_propose_patch_rejects_non_applying_diff(workspace: agent.Workspace) -> None:
    bad = GOOD_DIFF.replace("return a + b", "return x + y", 1)
    assert workspace.propose_patch(bad).startswith("rejected")
    assert workspace.last_applied_diff is None


def test_worker_target_filter() -> None:
    allowed = ["tests", "tests/", "tests/test_a.py", "tests/test_a.py::C::test_x"]
    for target in allowed:
        assert worker.TARGET_RE.match(target)
    for target in ("../etc", "tests;rm -rf", "app/mod.py", "tests/a.py --co -x", "-m pytest"):
        assert not worker.TARGET_RE.match(target)


def test_worker_rejects_tree_outside_root(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "request.json").write_text(
        json.dumps({"tree": str(tmp_path), "target": "tests"}), encoding="utf-8"
    )
    assert worker.run_job(job, Path("/nonexistent/python")) == {
        "error": "tree outside the allowed root"
    }


def test_blind_json_extraction() -> None:
    structured = json.dumps({"structured_output": {"diagnosis": "d", "patch": "p"}})
    assert blind._extract_json(structured) == {"diagnosis": "d", "patch": "p"}
    wrapped = json.dumps({"result": 'text {"diagnosis": "d", "patch": "p"} tail'})
    assert blind._extract_json(wrapped)["patch"] == "p"
    with pytest.raises(Exception, match="no structured JSON"):
        blind._extract_json(json.dumps({"result": "no json here"}))
