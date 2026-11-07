"""Validate mined cases in fresh, isolated Python environments."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from benchmarks.real_bugs.common import (
    CACHE_ROOT,
    CASE_ROOT,
    PREPARED_ROOT,
    VENV_ROOT,
    atomic_json,
    git,
    run,
    safe_case_path,
    validate_case_metadata,
)

PREPARATION_SCHEMA_VERSION = 5


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _make_venv(case: dict[str, Any], temp: Path, venv: Path) -> tuple[Path, Path]:
    if venv.exists():
        shutil.rmtree(venv)
    run([sys.executable, "-m", "venv", str(venv)], timeout=120)
    python = _venv_python(venv)
    requirements = ["pytest==8.4.2", *case.get("test_dependencies", [])]
    if len(set(requirements)) != len(requirements):
        requirements = list(dict.fromkeys(requirements))
    wheelhouse = temp / "wheelhouse"
    wheelhouse.mkdir()
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(wheelhouse),
            *requirements,
        ],
        timeout=300,
    )
    run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            *requirements,
        ],
        timeout=300,
    )
    return venv, python


def _apply_patch(repository: Path, patch: bytes) -> None:
    patch_file = repository.parent / "case.patch"
    patch_file.write_bytes(patch)
    run(["git", "apply", "--check", str(patch_file)], cwd=repository, timeout=30)
    run(["git", "apply", str(patch_file)], cwd=repository, timeout=30)


def _assert_complete_checkout(repository: Path) -> None:
    missing = git("ls-files", "--deleted", cwd=repository).splitlines()
    if missing:
        sample = ", ".join(missing[:5])
        raise RuntimeError(
            f"historical checkout is missing {len(missing)} tracked files; "
            f"rehydrate the repository cache before preparing cases (for example: {sample})"
        )


def _test_harness_source() -> str:
    """Trusted runner that repeats baseline and fixed tests inside one sandbox."""
    return """from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

python = sys.argv[1]
timeout = float(sys.argv[2])
test_paths = json.loads(sys.argv[3])
source_paths = json.loads(sys.argv[4])
root = Path.cwd()

module_roots = set()
for source_path in source_paths:
    parts = Path(source_path).parts
    offset = 1 if parts and parts[0] == "src" else 0
    if offset < len(parts):
        module = parts[offset]
        if offset == len(parts) - 1 and module.endswith(".py"):
            module = Path(module).stem
        if module.isidentifier():
            module_roots.add(module)

def run_tests(repository: Path, paths: list[str]) -> dict[str, object]:
    python_paths = [repository / "src", repository]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in python_paths if path.is_dir()
    )
    if (repository / "tests" / "__init__.py").is_file():
        module_roots.add("tests")
    bootstrap = r'''import importlib, json, sys
from pathlib import Path
repo = Path.cwd()
sys.path[:0] = [str(path) for path in (repo / "src", repo) if path.is_dir()]
for module in json.loads(sys.argv[1]):
    imported = importlib.import_module(module)
    if module in {"test", "tests"} and (repo / module / "__init__.py").is_file():
        imported.__path__ = [str(repo / module)]
import pytest
raise SystemExit(pytest.main(sys.argv[2:]))
'''
    try:
        result = subprocess.run(
            [python, "-c", bootstrap, json.dumps(sorted(module_roots)), "-q", *paths],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        errors = exc.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if isinstance(errors, bytes):
            errors = errors.decode("utf-8", errors="replace")
        return {"passed": False, "timed_out": True, "output": (output + errors)[-2000:]}
    return {
        "passed": result.returncode == 0,
        "timed_out": False,
        "output": (result.stdout + result.stderr)[-2000:],
    }

before = [run_tests(root / "baseline", test_paths) for _ in range(3)]
if any(item["passed"] for item in before):
    print(json.dumps({"before_runs": before, "after_runs": [], "full_suite": None}))
    raise SystemExit(0)

after = [run_tests(root / "fixed", test_paths) for _ in range(3)]
full = run_tests(root / "fixed", []) if all(item["passed"] for item in after) else None
print(json.dumps({"before_runs": before, "after_runs": after, "full_suite": full}))
"""


async def _pytest_repeated(
    workspace: Path,
    venv: Path,
    python: Path,
    paths: list[str],
    source_paths: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run all baseline/fixed repetitions in one OS sandbox session."""
    from mcp_server.execution_sandbox import run_untrusted_command

    result = await run_untrusted_command(
        [
            sys.executable,
            "-c",
            _test_harness_source(),
            str(python),
            str(timeout_seconds),
            json.dumps(paths),
            json.dumps(source_paths),
        ],
        cwd=workspace,
        timeout=timeout_seconds * 7 + 30,
        read_only_paths=[venv],
    )
    if result.get("sandbox_unavailable"):
        return result
    try:
        summary = json.loads(str(result.get("output", "")))
    except json.JSONDecodeError:
        return {
            **result,
            "harness_error": "sandboxed test harness returned no valid summary",
        }
    if not result.get("passed"):
        return {
            **result,
            **summary,
            "harness_error": "sandboxed test harness did not complete successfully",
        }
    return {**result, **summary}


def _copy_clean_tree(source: Path, target: Path) -> None:
    blocked_names = {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules"}
    blocked_files = {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "credentials.json",
        "secret.json",
        "secrets.json",
        "token.json",
        "service-account.json",
    }
    blocked_suffixes = {".pem", ".p12", ".pfx", ".key"}
    source = source.resolve(strict=True)
    target.mkdir(parents=True, exist_ok=False)
    for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
        base = Path(root)
        relative = base.relative_to(source)
        dirs[:] = [
            name
            for name in dirs
            if name not in blocked_names
            and not (base / name).is_symlink()
            and not _is_junction(base / name)
        ]
        destination = target / relative
        destination.mkdir(parents=True, exist_ok=True)
        for name in files:
            candidate = base / name
            if (
                candidate.is_symlink()
                or _is_junction(candidate)
                or name.lower() in blocked_files
                or (name.lower().startswith(".env.") and name.lower() != ".env.example")
                or candidate.suffix.lower() in blocked_suffixes
            ):
                continue
            shutil.copy2(candidate, destination / name, follow_symlinks=False)


def _case_directories(root: Path) -> list[Path]:
    manifest = root / "manifest.json"
    if manifest.is_file():
        document = json.loads(manifest.read_text(encoding="utf-8"))
        identifiers = document.get("case_ids") if isinstance(document, dict) else None
        if not isinstance(identifiers, list) or any(
            not isinstance(item, str) or item in {".", ".."} or Path(item).name != item
            for item in identifiers
        ):
            raise ValueError(f"{manifest}: expected a list of safe case IDs")
        return [
            path
            for identifier in identifiers
            if (path := root / identifier).is_dir() and (path / "metadata.json").is_file()
        ]
    return sorted(
        path for path in root.iterdir() if path.is_dir() and (path / "metadata.json").is_file()
    )


def _eligibility(
    before_passed: list[bool], after_passed: list[bool] | None
) -> tuple[bool, str | None]:
    if len(before_passed) != 3 or (after_passed is not None and len(after_passed) != 3):
        raise ValueError("case preparation requires three baseline runs and three fixed runs")
    if any(before_passed):
        return False, "flaky_or_not_reproducing"
    if after_passed is not None and not all(after_passed):
        return False, "flaky_or_human_fix_did_not_pass"
    return True, None


def prepare_case(
    case_dir: Path,
    *,
    cache: Path = CACHE_ROOT,
    timeout: float = 90,
    prepared_root: Path = PREPARED_ROOT,
    venv_root: Path = VENV_ROOT,
    force: bool = False,
) -> dict[str, Any]:
    metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
    validate_case_metadata(metadata, expected_id=case_dir.name)
    repo_cache = cache / metadata["repository"]
    if not (repo_cache / ".git").is_dir():
        raise FileNotFoundError(f"mined git cache is missing: {repo_cache}")
    venv_id = metadata.get("venv_id", metadata["case_id"])
    if not isinstance(venv_id, str) or not safe_case_path(venv_id) or Path(venv_id).name != venv_id:
        raise ValueError("metadata has an invalid venv_id")
    prepared = prepared_root / metadata["case_id"] / "repo"
    previous = prepared_root / metadata["case_id"] / "prepared.json"
    if previous.is_file() and not force:
        existing = json.loads(previous.read_text(encoding="utf-8"))
        if existing.get("preparation_version") == PREPARATION_SCHEMA_VERSION:
            if existing.get("status") == "kept" and prepared.is_dir():
                return existing
            if existing.get("status") == "discarded" and existing.get("reason") not in {
                "sandbox_unavailable",
                "sandbox_harness_error",
            }:
                return existing
    shutil.rmtree(prepared.parent, ignore_errors=True)

    result: dict[str, Any] = {
        "case_id": metadata["case_id"],
        "venv_id": venv_id,
        "preparation_version": PREPARATION_SCHEMA_VERSION,
        "status": "discarded",
    }
    with tempfile.TemporaryDirectory(prefix="selfheal-realbug-prepare-") as temp_text:
        temp = Path(temp_text)
        checkout = temp / "checkout"
        run(
            ["git", "clone", "--quiet", "--no-checkout", str(repo_cache), str(checkout)],
            timeout=300,
        )
        git("checkout", "--quiet", "--detach", metadata["parent_sha"], cwd=checkout, timeout=120)
        _assert_complete_checkout(checkout)
        test_patch = (case_dir / "test.diff").read_bytes()
        human_patch_path = case_dir / "scoring_only" / "human_fix.diff"
        if not human_patch_path.is_file():
            raise FileNotFoundError(f"scoring-only human diff is missing for {metadata['case_id']}")
        human_patch = human_patch_path.read_bytes()
        _apply_patch(checkout, test_patch)
        # Freeze exactly what the model may see before the human fix is ever
        # applied. The scoring diff remains separate from this copy.
        model_checkout = temp / "model_checkout"
        _copy_clean_tree(checkout, model_checkout)
        venv = venv_root / venv_id
        venv.parent.mkdir(parents=True, exist_ok=True)
        python = _venv_python(_make_venv(metadata, temp, venv)[0])
        test_paths = metadata["test_paths"]
        sandbox_workspace = temp / "sandbox_workspace"
        sandbox_workspace.mkdir()
        baseline_checkout = sandbox_workspace / "baseline"
        fixed_checkout = sandbox_workspace / "fixed"
        _copy_clean_tree(checkout, baseline_checkout)
        _apply_patch(checkout, human_patch)
        _copy_clean_tree(checkout, fixed_checkout)

        test_evidence = asyncio.run(
            _pytest_repeated(
                sandbox_workspace,
                venv,
                python,
                test_paths,
                metadata["source_paths"],
                timeout,
            )
        )
        before_runs = test_evidence.get("before_runs", [])
        before_ok = [bool(item.get("passed")) for item in before_runs]
        if test_evidence.get("sandbox_unavailable"):
            result.update(
                status="discarded",
                reason="sandbox_unavailable",
                before_passed=before_ok,
                sandbox_output=str(test_evidence.get("output", ""))[-2000:],
            )
            shutil.rmtree(venv, ignore_errors=True)
            return _save_preparation(previous, result)
        if test_evidence.get("harness_error") or len(before_runs) != 3:
            result.update(
                status="discarded",
                reason="sandbox_harness_error",
                before_passed=before_ok,
                sandbox_output=str(test_evidence.get("output", ""))[-2000:],
            )
            shutil.rmtree(venv, ignore_errors=True)
            return _save_preparation(previous, result)
        eligible, reason = _eligibility(before_ok, None)
        if not eligible:
            result.update(
                status="discarded",
                reason=reason,
                before_passed=before_ok,
                before_outputs=[str(item.get("output", ""))[-2000:] for item in before_runs],
            )
            shutil.rmtree(venv, ignore_errors=True)
            return _save_preparation(previous, result)

        after_runs = test_evidence.get("after_runs", [])
        after_ok = [bool(item.get("passed")) for item in after_runs]
        if len(after_runs) != 3:
            result.update(
                status="discarded",
                reason="sandbox_harness_error",
                before_passed=before_ok,
                before_outputs=[str(item.get("output", ""))[-2000:] for item in before_runs],
                sandbox_output=str(test_evidence.get("output", ""))[-2000:],
            )
            shutil.rmtree(venv, ignore_errors=True)
            return _save_preparation(previous, result)
        eligible, reason = _eligibility(before_ok, after_ok)
        if not eligible:
            result.update(
                status="discarded",
                reason=reason,
                before_passed=before_ok,
                before_outputs=[str(item.get("output", ""))[-2000:] for item in before_runs],
                after_passed=after_ok,
                after_outputs=[str(item.get("output", ""))[-2000:] for item in after_runs],
            )
            shutil.rmtree(venv, ignore_errors=True)
            return _save_preparation(previous, result)

        # Full-suite evidence helps score extra checks, but it is not a case
        # eligibility condition: some historical commits had unrelated suite
        # failures. The target test criterion above remains mandatory.
        full_suite = test_evidence.get("full_suite") or {"passed": False, "not_run": True}
        _copy_clean_tree(model_checkout, prepared)
        result.update(
            status="kept",
            parent_sha=metadata["parent_sha"],
            test_paths=test_paths,
            test_passes_before=before_ok,
            test_passes_with_human_fix=after_ok,
            before_outputs=[str(item.get("output", ""))[-2000:] for item in before_runs],
            after_outputs=[str(item.get("output", ""))[-2000:] for item in after_runs],
            human_full_suite_pass=bool(full_suite.get("passed")),
            human_full_suite_output=str(full_suite.get("output", ""))[-2000:],
            extra_test_file_count=_count_extra_test_files(checkout, set(test_paths)),
            extra_checks_available=(
                bool(full_suite.get("passed"))
                and _count_extra_test_files(checkout, set(test_paths)) > 0
            ),
            sandbox=test_evidence.get("sandbox"),
        )
    return _save_preparation(previous, result)


def _count_extra_test_files(repository: Path, target_tests: set[str]) -> int:
    files = [
        path
        for path in repository.rglob("test_*.py")
        if ".git" not in path.parts and not any(part in {".venv", "venv"} for part in path.parts)
    ]
    return sum(path.relative_to(repository).as_posix() not in target_tests for path in files)


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker()) if checker is not None else False


def _save_preparation(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    atomic_json(path, result)
    return result


def prepare_all(
    *,
    cases: Path = CASE_ROOT,
    cache: Path = CACHE_ROOT,
    timeout: float = 90,
    prepared_root: Path = PREPARED_ROOT,
    venv_root: Path = VENV_ROOT,
    workers: int = 3,
    repository: str | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    if workers < 1 or workers > 8:
        raise ValueError("preparation workers must be between 1 and 8")
    case_dirs = _case_directories(cases)
    if repository is not None:
        case_dirs = [
            case
            for case in case_dirs
            if json.loads((case / "metadata.json").read_text(encoding="utf-8"))["repository"]
            == repository
        ]
    if workers == 1:
        return [
            prepare_case(
                case,
                cache=cache,
                timeout=timeout,
                prepared_root=prepared_root,
                venv_root=venv_root,
                force=force,
            )
            for case in case_dirs
        ]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {
            pool.submit(
                prepare_case,
                case,
                cache=cache,
                timeout=timeout,
                prepared_root=prepared_root,
                venv_root=venv_root,
                force=force,
            ): case
            for case in case_dirs
        }
        for future in as_completed(pending):
            results.append(future.result())
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASE_ROOT)
    parser.add_argument("--cache", type=Path, default=CACHE_ROOT)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--prepared", type=Path, default=PREPARED_ROOT)
    parser.add_argument("--venvs", type=Path, default=VENV_ROOT)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--repository", help="prepare only the named configured repository")
    parser.add_argument("--force", action="store_true", help="ignore completed prior results")
    args = parser.parse_args(argv)
    results = prepare_all(
        cases=args.cases,
        cache=args.cache,
        timeout=args.timeout,
        prepared_root=args.prepared,
        venv_root=args.venvs,
        workers=args.workers,
        repository=args.repository,
        force=args.force,
    )
    kept = sum(item["status"] == "kept" for item in results)
    print(f"Prepared {kept}/{len(results)} cases; discarded {len(results) - kept}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
