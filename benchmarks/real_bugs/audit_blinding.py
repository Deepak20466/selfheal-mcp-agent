"""Audit frozen real-bug model contexts for later-history leakage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.real_bugs.common import CACHE_ROOT, CASE_ROOT, PREPARED_ROOT, git
from benchmarks.real_bugs.run import SAFE_TOOL_NAMES, _benchmark_prompt, _git_init_snapshot
from healer.prompts import build_initial_messages

_CREDENTIAL_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(
        r"(?i)(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*['\"]?[^\s'\"]{16,}"
    ),
    re.compile(r"://[^/\s:@]+:[^@\s]+@"),
)
_INFORMATIVE_TERMS = re.compile(
    r"\b(?:bug|defect|fix(?:es|ed|ing)?|regression|issue|error|failure|fail(?:s|ed|ing)?|"
    r"crash(?:es|ed)?|security|vulnerabilit\w*|patch(?:es|ed)?|revert(?:s|ed)?|"
    r"workaround|repair(?:s|ed)?|resolv(?:e|es|ed|ing)|prevent(?:s|ed|ing)?|"
    r"test(?:s|ed|ing)?|broken|incorrect|wrong|missing|unexpected|support(?:s|ed)?|"
    r"CVE)\b",
    re.IGNORECASE,
)
_IDENTIFIER_PATTERN = re.compile(
    r"(?:#[0-9]+|\bCVE-[0-9]{4}-[0-9]+\b|\b[0-9a-f]{7,40}\b|"
    r"\b\d+\.\d+(?:\.\d+)?\b|[/\\]|\w[\w.]*\(\)|\b\w+_\w+\b)",
    re.IGNORECASE,
)
AUDIT_RULE = (
    "A later-commit message is a collision only when its complete text occurs case-insensitively, "
    "with runs of whitespace normalized and word boundaries around the message in the model "
    "context. A collision is GENERIC only when it has at most four "
    "whitespace-separated words, occurs as a whole phrase in ordinary prepared-repository "
    "text, contains none of the fixed defect/fix/test/error/security terms or issue/version/path/"
    "commit/code identifiers, and does not otherwise describe a defect, fix, or related issue. "
    "Ambiguous matches are INFORMATIVE. A case is retained only when all its matches are "
    "GENERIC; a case with no matches passes. Repeated identical messages are deduplicated per "
    "case while retaining the count and union of locations."
)


def _contains_credential_like_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS)


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _contains_normalized_phrase(phrase: str, text: str) -> bool:
    normalized_phrase = _normalize_text(phrase)
    if not normalized_phrase:
        return False
    start = text.find(normalized_phrase)
    while start >= 0:
        end = start + len(normalized_phrase)
        left_is_word = start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_")
        right_is_word = end < len(text) and (text[end].isalnum() or text[end] == "_")
        if not left_is_word and not right_is_word:
            return True
        start = text.find(normalized_phrase, start + 1)
    return False


def _contains_phrase(phrase: str, values: list[str]) -> bool:
    normalized_phrase = _normalize_text(phrase)
    return any(
        _contains_normalized_phrase(normalized_phrase, _normalize_text(value)) for value in values
    )


def _message_texts(messages: Any) -> list[str]:
    if isinstance(messages, str):
        return [messages]
    if isinstance(messages, list):
        return [text for item in messages for text in _message_texts(item)]
    if isinstance(messages, dict):
        return [
            text
            for key in ("content", "text")
            if key in messages
            for text in _message_texts(messages[key])
        ]
    return []


def _classify_match(
    text: str,
    normalized_files: list[tuple[str, str]],
) -> tuple[str, str]:
    words = text.split()
    reasons: list[str] = []
    if len(words) > 4:
        reasons.append("more than four words")
    if _INFORMATIVE_TERMS.search(text):
        reasons.append("contains defect/fix/test/error/security terminology")
    if _IDENTIFIER_PATTERN.search(text):
        reasons.append("contains an issue, version, path, commit, or code identifier")
    phrase = _normalize_text(text)
    if not any(_contains_normalized_phrase(phrase, content) for _, content in normalized_files):
        reasons.append("no whole-phrase occurrence in ordinary repository text")
    if reasons:
        return "INFORMATIVE", "; ".join(reasons)
    return (
        "GENERIC",
        "at most four words; occurs as a whole phrase in repository text; "
        "no defect/fix/test/error/security terms or technical identifiers",
    )


def _commit_messages(repository: Path) -> dict[str, str]:
    log = git("log", "--all", "--format=%H%x1e%B%x1f", cwd=repository)
    messages: dict[str, str] = {}
    for record in log.split("\x1f"):
        record = record.strip("\r\n")
        if not record:
            continue
        commit, separator, message = record.partition("\x1e")
        if separator:
            messages[commit] = message.strip()
    return messages


def _later_history(
    repository: Path, fix_sha: str, messages: dict[str, str]
) -> list[tuple[str, str]]:
    graph = git("rev-list", "--all", "--parents", cwd=repository).splitlines()
    children: dict[str, list[str]] = {}
    for line in graph:
        parts = line.split()
        if not parts:
            continue
        commit, parents = parts[0], parts[1:]
        for parent in parents:
            children.setdefault(parent, []).append(commit)

    descendants: set[str] = set()
    pending = list(children.get(fix_sha, []))
    while pending:
        commit = pending.pop()
        if commit in descendants:
            continue
        descendants.add(commit)
        pending.extend(children.get(commit, []))

    return [(commit, messages.get(commit, "")) for commit in sorted(descendants)]


def _text_files(root: Path) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        files.append((path.relative_to(root).as_posix(), content))
    return files


def _stratified_sample(case_ids: list[str], sample_size: int) -> list[str]:
    """Select a reproducible sample spread across source repositories."""
    groups: dict[str, list[str]] = {}
    for identifier in case_ids:
        metadata = json.loads(
            (CASE_ROOT / identifier / "metadata.json").read_text(encoding="utf-8")
        )
        groups.setdefault(str(metadata["repository"]), []).append(identifier)
    for group in groups.values():
        group.sort()
    selected: list[str] = []
    repositories = sorted(groups)
    target = min(sample_size, len(case_ids))
    while len(selected) < target:
        for repository in repositories:
            if groups[repository]:
                selected.append(groups[repository].pop(0))
                if len(selected) == target:
                    break
    return selected


def audit(config_path: Path, output_path: Path, *, sample_size: int = 5) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    case_ids = config.get("case_ids") if isinstance(config, dict) else None
    if not isinstance(case_ids, list) or not case_ids:
        raise ValueError("frozen config must contain a non-empty case_ids list")
    sample = _stratified_sample(case_ids, sample_size)
    checks: list[dict[str, Any]] = []

    for identifier in sample:
        case_dir = CASE_ROOT / identifier
        prepared_dir = PREPARED_ROOT / identifier
        metadata = json.loads((case_dir / "metadata.json").read_text(encoding="utf-8"))
        prepared = json.loads((prepared_dir / "prepared.json").read_text(encoding="utf-8"))
        if prepared.get("status") != "kept":
            raise ValueError(f"frozen sample {identifier!r} is not kept")

        fix_sha = metadata["fix_sha"]
        human_diff = (case_dir / "scoring_only" / "human_fix.diff").read_text(encoding="utf-8")
        repository = CACHE_ROOT / metadata["repository"]
        messages_by_commit = _commit_messages(repository)
        fix_message = messages_by_commit.get(fix_sha)
        if fix_message is None:
            raise ValueError(f"fixing commit is missing from local history for {identifier}")
        later_history = _later_history(repository, fix_sha, messages_by_commit)

        with tempfile.TemporaryDirectory(prefix="real-bug-blinding-") as temp_dir:
            snapshot = Path(temp_dir) / "model-repository"
            _git_init_snapshot(prepared_dir / "repo", snapshot)
            traceback = "\n".join(str(item) for item in prepared.get("before_outputs", []))
            source_path = metadata["source_paths"][0]
            source = {
                "id": 1,
                "fingerprint": hashlib.sha256(f"{identifier}:with_validation".encode()).hexdigest(),
                "exception_type": "AssertionError",
                "message": "Historical regression test fails on the selected parent commit.",
                "traceback": traceback,
                "file_path": source_path,
                "line_number": 1,
                "function_name": Path(source_path).stem,
                "status": "open",
                "occurrence_count": 1,
                "first_seen_at": "2026-01-01T00:00:00+00:00",
                "last_seen_at": "2026-01-01T00:00:00+00:00",
            }
            messages = build_initial_messages(
                source_kind="error",
                source=source,
                attempt_number=1,
                max_attempts=1,
                heal_job_id=1,
                worktree=f"realbug-{identifier}-with_validation",
                previous_attempts_summary=None,
            )
            files = _text_files(snapshot)
            normalized_files = [(path, _normalize_text(content)) for path, content in files]
            normalized_repository = "\0".join(content for _, content in normalized_files)
            history = git("log", "--all", "--format=%H%x09%s", cwd=snapshot).splitlines()
            system_context = _benchmark_prompt()
            user_message_texts = _message_texts(messages)
            normalized_system = _normalize_text(system_context)
            normalized_user_messages = [_normalize_text(value) for value in user_message_texts]
            context = json.dumps(
                {
                    "system": _benchmark_prompt(),
                    "messages": messages,
                    "tool_names": sorted(SAFE_TOOL_NAMES),
                    "files": files,
                    "snapshot_history": history,
                },
                sort_keys=True,
            )
            diff_exists = any(
                path.name == "metadata.json"
                or path.name.endswith(".diff")
                or "scoring_only" in path.parts
                for path in snapshot.rglob("*")
            )
            later_hashes = [commit for commit, _message in later_history]
            later_messages = [message for _commit, message in later_history if message]
            later_message_counts = Counter(later_messages)
            hash_absent = fix_sha not in context and not any(
                token in context for token in later_hashes
            )
            fix_message_absent = not _contains_phrase(
                _normalize_text(fix_message),
                [normalized_system, *normalized_user_messages]
                + [content for _, content in normalized_files],
            )
            later_message_collisions_by_text: dict[str, dict[str, Any]] = {}
            for token, occurrence_count in later_message_counts.items():
                if not token:
                    continue
                normalized_token = _normalize_text(token)
                system_match = _contains_normalized_phrase(normalized_token, normalized_system)
                initial_match = any(
                    _contains_normalized_phrase(normalized_token, content)
                    for content in normalized_user_messages
                )
                repository_match = _contains_normalized_phrase(
                    normalized_token, normalized_repository
                )
                if not (system_match or initial_match or repository_match):
                    continue
                matched_paths = (
                    [
                        path
                        for path, content in normalized_files
                        if _contains_normalized_phrase(normalized_token, content)
                    ]
                    if repository_match
                    else []
                )
                match = later_message_collisions_by_text.setdefault(
                    token,
                    {
                        "text": token,
                        "word_count": len(token.split()),
                        "occurrence_count": 0,
                        "system_prompt": False,
                        "initial_user_messages": False,
                        "repository_paths": set(),
                    },
                )
                match["occurrence_count"] += occurrence_count
                match["system_prompt"] |= system_match
                match["initial_user_messages"] |= initial_match
                match["repository_paths"].update(matched_paths)
            later_message_collisions = []
            for match in later_message_collisions_by_text.values():
                match["repository_paths"] = sorted(match["repository_paths"])
                match["classification"], match["classification_reason"] = _classify_match(
                    match["text"], normalized_files
                )
                later_message_collisions.append(match)
            human_diff_absent = human_diff not in context
            metadata_hashes_absent = all(
                token not in context
                for token in (metadata.get("patch_sha256", ""), metadata.get("subject_sha256", ""))
                if token
            )
            checked_tokens = 1 + len(later_hashes) + 1 + len(later_messages) + 1 + 2
            only_synthetic_history = len(history) == 1 and "Benchmark parent snapshot" in history[0]
            no_history_tools = not any(
                name in SAFE_TOOL_NAMES
                for name in {"get_recent_commits", "get_git_blame", "search_code"}
            )
            checks.append(
                {
                    "case_id": identifier,
                    "fix_and_later_commit_hashes_absent": hash_absent,
                    "fix_commit_message_absent": fix_message_absent,
                    "later_git_history_absent": only_synthetic_history,
                    "human_fix_diff_absent": human_diff_absent,
                    "metadata_hashes_absent": metadata_hashes_absent,
                    "scoring_metadata_and_diff_absent": not diff_exists,
                    "snapshot_contains_only_synthetic_commit": only_synthetic_history,
                    "history_tools_not_exposed": no_history_tools,
                    "forbidden_token_count_checked": checked_tokens,
                    "fix_message_in_initial_prompt": _contains_phrase(
                        _normalize_text(fix_message),
                        [normalized_system, *normalized_user_messages],
                    ),
                    "fix_message_in_repository_files": _contains_phrase(
                        _normalize_text(fix_message), [content for _, content in normalized_files]
                    ),
                    "later_message_in_initial_prompt": any(
                        _contains_normalized_phrase(_normalize_text(token), normalized_system)
                        or any(
                            _contains_normalized_phrase(_normalize_text(token), content)
                            for content in normalized_user_messages
                        )
                        for token in later_message_counts
                    ),
                    "later_message_in_repository_files": any(
                        _contains_normalized_phrase(_normalize_text(token), normalized_repository)
                        for token in later_message_counts
                    ),
                    "later_message_text_collision_count": len(later_message_collisions),
                    "later_message_text_collisions": later_message_collisions,
                    "later_message_strings_absent_from_model_context": not later_message_collisions,
                    "no_informative_message_leakage": all(
                        match["classification"] == "GENERIC" for match in later_message_collisions
                    ),
                }
            )

    if any(
        _contains_credential_like_text(match["text"])
        for item in checks
        for match in item["later_message_text_collisions"]
    ):
        raise RuntimeError("Credential-like content detected; audit findings were not saved.")

    def core_passed(item: dict[str, Any]) -> bool:
        return all(
            item[key]
            for key in (
                "fix_and_later_commit_hashes_absent",
                "fix_commit_message_absent",
                "later_git_history_absent",
                "human_fix_diff_absent",
                "metadata_hashes_absent",
                "scoring_metadata_and_diff_absent",
                "snapshot_contains_only_synthetic_commit",
                "history_tools_not_exposed",
            )
        )

    kept_case_ids = [
        item["case_id"]
        for item in checks
        if core_passed(item) and item["no_informative_message_leakage"]
    ]
    dropped_cases = [
        {
            "case_id": item["case_id"],
            "reasons": sorted(
                {
                    match["classification_reason"]
                    for match in item["later_message_text_collisions"]
                    if match["classification"] == "INFORMATIVE"
                }
                | ({"core blinding check failed"} if not core_passed(item) else set())
            ),
        }
        for item in checks
        if item["case_id"] not in kept_case_ids
    ]

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "sample_size": len(sample),
        "sample_case_ids": sample,
        "checks": checks,
        "audit_rule": AUDIT_RULE,
        "kept_case_ids": kept_case_ids,
        "dropped_cases": dropped_cases,
        "passed": not dropped_cases,
        "method": (
            "Audited every case ID in the supplied config. Built the same initial user message "
            "and system prompt as the benchmark, scanned all readable workspace files, and "
            "checked fixing-commit metadata, human scoring artifacts, model-visible Git history, "
            "safe tool names, and complete later-message strings. Applied the written audit rule "
            "uniformly; any informative or ambiguous match drops its case."
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=41)
    args = parser.parse_args()
    if args.sample_size <= 0:
        parser.error("--sample-size must be positive")
    report = audit(args.config, args.output, sample_size=args.sample_size)
    outcome = "PASS" if report["passed"] else "FAIL"
    print(f"Blinding audit: {outcome} ({report['sample_size']} cases)")
    print(f"Saved audit report: {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
