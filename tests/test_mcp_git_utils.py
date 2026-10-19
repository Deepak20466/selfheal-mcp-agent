"""mcp_server.git_utils: git blame/log/apply and pytest-in-a-worktree.

Runs against the real repo (read-only ops) and a real, disposable
`git worktree` (write ops) via the `git_worktree` fixture.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from mcp_server import git_utils
from mcp_server.sandbox import REPO_ROOT


async def test_recent_commits_returns_real_history() -> None:
    commits = await git_utils.recent_commits(2, cwd=REPO_ROOT)
    assert len(commits) == 2
    for commit in commits:
        assert len(commit["sha"]) == 40
        assert commit["author"]
        assert commit["subject"]


async def test_blame_line_on_a_real_tracked_file() -> None:
    info = await git_utils.blame_line("SPEC.md", 1, cwd=REPO_ROOT)
    assert len(info["commit"]) == 40
    assert "author" in info


async def test_blame_line_on_a_nonexistent_file_raises() -> None:
    with pytest.raises(git_utils.GitCommandError):
        await git_utils.blame_line("does/not/exist.py", 1, cwd=REPO_ROOT)


def _real_diff_for_prepending_a_line(worktree_path: Path, relative_path: str, line: str) -> str:
    """Make a real edit, capture `git diff` for it, then revert - so the
    returned text is a guaranteed-valid unified diff (not hand-crafted)."""
    target_file = worktree_path / relative_path
    original = target_file.read_text(encoding="utf-8")
    target_file.write_text(line + original, encoding="utf-8")

    diff_text = subprocess.run(  # noqa: S603
        ["git", "diff", "--", relative_path],  # noqa: S607
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    subprocess.run(  # noqa: S603
        ["git", "checkout", "--", relative_path],  # noqa: S607
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
    )
    assert target_file.read_text(encoding="utf-8") == original
    return diff_text


async def test_apply_diff_and_run_pytest_in_a_real_worktree(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    relative_path = "apps/target_app/seed_data.py"
    diff_text = _real_diff_for_prepending_a_line(
        worktree_path, relative_path, "# a harmless marker comment added by a test\n"
    )

    await git_utils.apply_diff(diff_text, cwd=worktree_path, check_only=True)
    await git_utils.apply_diff(diff_text, cwd=worktree_path, check_only=False)

    updated = (worktree_path / relative_path).read_text(encoding="utf-8")
    assert updated.startswith("# a harmless marker comment added by a test\n")

    stat = await git_utils.diff_stat(cwd=worktree_path)
    assert "seed_data.py" in stat


async def test_apply_diff_reverse_undoes_the_patch(git_worktree: tuple[str, Path]) -> None:
    _, worktree_path = git_worktree
    relative_path = "apps/target_app/seed_data.py"
    diff_text = _real_diff_for_prepending_a_line(worktree_path, relative_path, "# marker\n")

    await git_utils.apply_diff(diff_text, cwd=worktree_path, check_only=False)
    assert (worktree_path / relative_path).read_text(encoding="utf-8").startswith("# marker\n")

    await git_utils.apply_diff(diff_text, cwd=worktree_path, reverse=True)

    stat = await git_utils.diff_stat(cwd=worktree_path)
    assert stat.strip() == ""  # working tree is clean again


async def test_apply_diff_rejects_a_diff_that_does_not_match(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    bogus_diff = (
        "--- a/apps/target_app/seed_data.py\n"
        "+++ b/apps/target_app/seed_data.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-this line definitely does not exist in the file\n"
        "+neither does this replacement\n"
    )
    with pytest.raises(git_utils.GitCommandError):
        await git_utils.apply_diff(bogus_diff, cwd=worktree_path, check_only=True)


async def test_apply_diff_recounts_inaccurate_hunk_counts(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def value():\n    return 1\n", encoding="utf-8")
    patch = (
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,8 +1,9 @@\n"
        " def value():\n"
        "-    return 1\n"
        "+    return 2\n"
    )

    await git_utils.apply_diff(patch, cwd=tmp_path)

    assert target.read_text(encoding="utf-8") == "def value():\n    return 2\n"


async def test_apply_diff_timeout_kills_git_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class HangingProcess:
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            return self.returncode or 0

    process = HangingProcess()

    async def create_process(*args: object, **kwargs: object) -> HangingProcess:
        del args, kwargs
        return process

    monkeypatch.setattr(git_utils, "GIT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(git_utils.GitCommandError, match="git apply timed out"):
        await git_utils.apply_diff("diff", cwd=tmp_path)

    assert process.killed is True
    assert process.waited is True


async def test_apply_diff_accepts_lf_patch_for_crlf_checkout(
    tmp_path: Path,
) -> None:
    from core.git_env import git_subprocess_env

    source = tmp_path / "connected-repo"
    source.mkdir()
    (source / "app.py").write_bytes(b"VALUE = 1\r\n")
    env, _ = git_subprocess_env()

    async def run_git(*args: str, cwd: Path) -> None:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode(errors="replace")

    for args in (
        ["init", "-b", "main"],
        ["add", "-A"],
        ["commit", "-m", "initial"],
    ):
        await run_git(*args, cwd=source)
    worktree_path = tmp_path / "worktree"
    await run_git("clone", str(source), str(worktree_path), cwd=tmp_path)
    target = worktree_path / "app.py"
    assert b"\r\n" in target.read_bytes()
    diff_text = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    new_line = "VALUE = 2"

    await git_utils.apply_diff(diff_text, cwd=worktree_path)

    assert target.read_text(encoding="utf-8").splitlines()[0] == new_line


async def test_run_pytest_passes_for_a_trivially_passing_test(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    scratch_test = worktree_path / "apps" / "target_app" / "test_mcp_git_utils_scratch.py"
    scratch_test.write_text("def test_always_passes():\n    assert True\n", encoding="utf-8")

    result = await git_utils.run_pytest(
        "apps/target_app/test_mcp_git_utils_scratch.py", cwd=worktree_path
    )

    assert result["passed"] is True, result
    assert result["timed_out"] is False


async def test_run_pytest_reports_failure_for_a_failing_test(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    scratch_test = worktree_path / "apps" / "target_app" / "test_mcp_git_utils_scratch_fail.py"
    scratch_test.write_text("def test_always_fails():\n    assert False\n", encoding="utf-8")

    result = await git_utils.run_pytest(
        "apps/target_app/test_mcp_git_utils_scratch_fail.py", cwd=worktree_path
    )

    assert result["passed"] is False
    assert "test_always_fails" in result["output"]
