"""Find historical fix commits that pair small Python changes with tests."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any

from benchmarks.real_bugs.common import (
    CACHE_ROOT,
    CASE_ROOT,
    MAX_PATCH_BYTES,
    ROOT,
    atomic_json,
    case_id,
    git,
    load_repositories,
    run_bytes,
    safe_case_path,
)

_FIX_WORD = re.compile(r"\bfix(?:es|ed|ing)?\b", re.IGNORECASE)


def _clone_repository(name: str, url: str, cache: Path) -> Path:
    destination = cache / name
    if destination.joinpath(".git").is_dir():
        # Preparation clones old commits from this cache. A blobless cache
        # cannot serve missing historical files to a local clone, so hydrate
        # every blob before treating it as a checkout source.
        git(
            "fetch",
            "--quiet",
            "--all",
            "--tags",
            "--no-filter",
            "--refetch",
            cwd=destination,
            timeout=600,
        )
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--quiet", url, str(destination), timeout=600, cwd=cache)
    return destination


def _paths_changed(repo: Path, commit: str) -> list[str]:
    raw = git("diff-tree", "--no-commit-id", "--name-only", "-r", commit, cwd=repo)
    paths = [line.strip() for line in raw.splitlines() if line.strip()]
    if not all(safe_case_path(path) for path in paths):
        return []
    return paths


def _is_test(path: str) -> bool:
    """Recognize pytest test modules without misclassifying test helpers."""
    name = Path(path).name.lower()
    return (
        name in {"test.py", "tests.py"}
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.py"))
    )


def _diff(repo: Path, parent: str, commit: str, paths: list[str]) -> bytes:
    if not paths:
        return b""
    return run_bytes(
        ["git", "diff", "--binary", "--no-ext-diff", parent, commit, "--", *paths],
        cwd=repo,
        timeout=60,
    )


def mine(
    *,
    repos_file: Path = ROOT / "repos.yaml",
    cache: Path = CACHE_ROOT,
    output: Path = CASE_ROOT,
    limit_per_repo: int = 500,
) -> list[str]:
    """Mine candidates and save the two diffs in separate case artifacts."""
    candidates_written: list[str] = []
    for config in load_repositories(repos_file):
        name = config["name"]
        repository = _clone_repository(name, config["url"], cache)
        lines = git(
            "log",
            "--all",
            "--no-merges",
            "--regexp-ignore-case",
            "--grep=fix",
            "--format=%H%x09%P%x09%s",
            cwd=repository,
            timeout=180,
        ).splitlines()
        considered = 0
        for line in lines:
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            fix_sha, parents, subject = parts
            if not _FIX_WORD.search(subject):
                continue
            parent_list = parents.split()
            if len(parent_list) != 1:
                continue
            parent_sha = parent_list[0]
            paths = _paths_changed(repository, fix_sha)
            tests = [path for path in paths if _is_test(path)]
            sources = [path for path in paths if path.endswith(".py") and not _is_test(path)]
            if not tests or not sources or len(sources) > 3:
                continue
            test_patch = _diff(repository, parent_sha, fix_sha, tests)
            human_patch = _diff(repository, parent_sha, fix_sha, sources)
            if (
                not test_patch
                or not human_patch
                or len(test_patch) > MAX_PATCH_BYTES
                or len(human_patch) > MAX_PATCH_BYTES
            ):
                continue

            identifier = case_id(name, fix_sha)
            case_dir = output / identifier
            case_dir.mkdir(parents=True, exist_ok=True)
            metadata: dict[str, Any] = {
                "case_id": identifier,
                "repository": name,
                "repository_url": config["url"],
                "parent_sha": parent_sha,
                "fix_sha": fix_sha,
                "source_paths": sources,
                "test_paths": tests,
                "source_file_count": len(sources),
                "test_command": config["test_command"],
                "test_dependencies": config.get("test_dependencies", []),
                "patch_sha256": hashlib.sha256(human_patch).hexdigest(),
                "subject_sha256": hashlib.sha256(subject.encode()).hexdigest(),
            }
            atomic_json(case_dir / "metadata.json", metadata)
            (case_dir / "test.diff").write_bytes(test_patch)
            # Scoring material is kept outside every prepared/model workspace.
            scoring_dir = case_dir / "scoring_only"
            scoring_dir.mkdir(exist_ok=True)
            (scoring_dir / "human_fix.diff").write_bytes(human_patch)
            candidates_written.append(identifier)
            considered += 1
            if considered >= limit_per_repo:
                break
    atomic_json(output / "manifest.json", {"case_ids": candidates_written})
    return candidates_written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", type=Path, default=ROOT / "repos.yaml")
    parser.add_argument("--cache", type=Path, default=CACHE_ROOT)
    parser.add_argument("--output", type=Path, default=CASE_ROOT)
    parser.add_argument("--limit-per-repo", type=int, default=500)
    args = parser.parse_args(argv)
    identifiers = mine(
        repos_file=args.repos,
        cache=args.cache,
        output=args.output,
        limit_per_repo=args.limit_per_repo,
    )
    print(f"Candidates saved: {len(identifiers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
