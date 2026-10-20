"""Free-model-only inference and patch boundary regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import openrouter_repair
from scripts.repair_patch_policy import PatchRejected, validate_patch

PATCH = b"""diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-return 1
+return 2
"""
TEST_API_KEY = "sk-or-v1-" + "x" * 32


def _free_key_info() -> dict[str, object]:
    return {
        "data": {
            "is_free_tier": True,
            "is_management_key": False,
            "is_provisioning_key": False,
        }
    }


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src" / "app.py").write_text("return 1\n", encoding="utf-8")
    return repository


def test_source_only_existing_python_patch_is_accepted(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    digest = validate_patch(PATCH, repository)

    assert len(digest) == 64


def test_prompt_limits_model_to_one_minimal_implementation_file() -> None:
    prompt = openrouter_repair._prompt("source", "failure", None)

    assert "smallest possible correction in exactly one" in prompt
    assert "do not touch any other file" in prompt


@pytest.mark.parametrize(
    "patch",
    [
        PATCH.replace(b"src/app.py", b"../outside.py"),
        PATCH.replace(b"src/app.py", b"tests/test_app.py"),
        PATCH.replace(b"src/app.py", b".github/workflows/ci.yml"),
        PATCH.replace(b"src/app.py", b"src/app.js"),
        PATCH.replace(
            b"+return 2", b"+OPENROUTER_API_KEY='sk-or-v1-123456789012345678901234567890'"
        )
        .replace(b"@@ -1 +1 @@", b"@@ -1 +1 @@")
        .replace(b"-return 1", b"-return 1"),
    ],
)
def test_unsafe_or_out_of_scope_patch_is_rejected(tmp_path: Path, patch: bytes) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(PatchRejected):
        validate_patch(patch, repository, check_apply=False)


def test_symlink_patch_target_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("return 1\n", encoding="utf-8")
    (repository / "src" / "app.py").unlink()
    try:
        (repository / "src" / "app.py").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"this Windows account cannot create symbolic links: {exc}")

    with pytest.raises(PatchRejected, match="symbolic link"):
        validate_patch(PATCH, repository, check_apply=False)


def test_unified_diff_headers_must_match_git_diff_header(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    mismatched = PATCH.replace(b"+++ b/src/app.py", b"+++ b/src/other.py")

    with pytest.raises(PatchRejected, match="new file path do not match"):
        validate_patch(mismatched, repository, check_apply=False)


def test_artifact_size_and_binary_limits_are_enforced(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(PatchRejected, match="64 KiB"):
        validate_patch(PATCH + b" " * (64 * 1024), repository, check_apply=False)
    with pytest.raises(PatchRejected, match="binary"):
        validate_patch(PATCH + b"\x00", repository, check_apply=False)


def test_deletion_lines_that_resemble_diff_headers_still_count_toward_limit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    body = b"".join(b"--- removed source\n" for _ in range(201))
    patch = (
        b"diff --git a/src/app.py b/src/app.py\n"
        b"--- a/src/app.py\n"
        b"+++ b/src/app.py\n"
        b"@@ -1,201 +1 @@\n" + body + b"+return 2\n"
    )

    with pytest.raises(PatchRejected, match="200 changed-line"):
        validate_patch(patch, repository, check_apply=False)


def test_free_model_must_be_listed_and_zero_priced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        openrouter_repair,
        "_request",
        lambda *_args, **_kwargs: {
            "data": [
                {
                    "id": "vendor/model:free",
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        },
    )

    openrouter_repair.verify_free_model("vendor/model:free", TEST_API_KEY)

    with pytest.raises(openrouter_repair.InferenceError, match="ending in :free"):
        openrouter_repair.verify_free_model("vendor/model", TEST_API_KEY)
    with pytest.raises(openrouter_repair.InferenceError, match="not currently listed"):
        openrouter_repair.verify_free_model("vendor/other:free", TEST_API_KEY)


def test_free_endpoint_check_requires_live_zero_price_for_exact_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def endpoint_response(url: str, **_kwargs: object) -> dict[str, object]:
        calls.append(url)
        return {
            "data": {
                "id": "vendor/model:free",
                "endpoints": [
                    {"status": 1, "pricing": {"prompt": "0", "completion": "0"}},
                    {"status": 0, "pricing": {"prompt": "0", "completion": "0"}},
                ],
            }
        }

    monkeypatch.setattr(openrouter_repair, "_request", endpoint_response)
    openrouter_repair.verify_free_endpoint("vendor/model:free", TEST_API_KEY)
    assert calls == [f"{openrouter_repair.API_ROOT}/models/vendor/model%3Afree/endpoints"]

    monkeypatch.setattr(
        openrouter_repair,
        "_request",
        lambda *_args, **_kwargs: {
            "data": {
                "id": "vendor/model:free",
                "endpoints": [{"status": 0, "pricing": {"prompt": "0", "completion": "0.001"}}],
            }
        },
    )
    with pytest.raises(
        openrouter_repair.InferenceError, match="no currently available zero-priced"
    ):
        openrouter_repair.verify_free_endpoint("vendor/model:free", TEST_API_KEY)


def test_documented_free_tier_key_metadata_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def key_response(url: str, **_kwargs: object) -> dict[str, object]:
        calls.append(url)
        return _free_key_info()

    monkeypatch.setattr(openrouter_repair, "_request", key_response)
    openrouter_repair.verify_free_tier_key(TEST_API_KEY)
    assert calls == [f"{openrouter_repair.API_ROOT}/key"]

    calls.clear()
    monkeypatch.setattr(
        openrouter_repair,
        "_request",
        lambda url, **_kwargs: calls.append(url) or {"data": {"is_free_tier": False}},
    )
    with pytest.raises(openrouter_repair.InferenceError, match="Free-tier key"):
        openrouter_repair.verify_free_tier_key(TEST_API_KEY)
    assert calls == [f"{openrouter_repair.API_ROOT}/key"]


def test_rate_limit_response_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class RateLimitedOpener:
        def open(self, *_args: object, **_kwargs: object) -> None:
            nonlocal calls
            calls += 1
            raise openrouter_repair.urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                429,
                "rate limited",
                {"Retry-After": "7"},
                None,
            )

    monkeypatch.setattr(
        openrouter_repair.urllib.request,
        "build_opener",
        lambda *_handlers: RateLimitedOpener(),
    )

    with pytest.raises(openrouter_repair.RateLimitedError, match="HTTP 429") as limited:
        openrouter_repair._request(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            body=b"{}",
        )

    assert calls == 1
    assert limited.value.retry_after == 7


def test_rate_limit_waits_only_for_retry_after_and_live_free_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("CI failed", encoding="utf-8")
    calls: list[str] = []
    waits: list[int] = []

    def fake_request(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        calls.append(url)
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [{"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}]
            }
        if url.endswith("/endpoints"):
            return {
                "data": {
                    "id": "vendor/model:free",
                    "endpoints": [{"status": 0, "pricing": {"prompt": "0", "completion": "0"}}],
                }
            }
        if calls.count(url) == 1:
            raise openrouter_repair.RateLimitedError(2)
        return {
            "model": "vendor/model:free",
            "usage": {"cost": 0, "is_byok": False},
            "choices": [{"message": {"content": PATCH.decode("utf-8")}}],
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")
    monkeypatch.setattr(openrouter_repair, "_request", fake_request)
    monkeypatch.setattr(openrouter_repair.time, "sleep", waits.append)

    output = tmp_path / "patch.diff"
    openrouter_repair.generate_patch(repository, failure, output)

    assert output.read_bytes() == PATCH
    assert waits == [2]
    assert calls.count(f"{openrouter_repair.API_ROOT}/chat/completions") == 2
    assert calls.index(f"{openrouter_repair.API_ROOT}/models/vendor/model%3Afree/endpoints") < max(
        i for i, call in enumerate(calls) if call.endswith("/chat/completions")
    )


def test_rate_limit_without_retry_after_stops_without_second_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("CI failed", encoding="utf-8")
    completions = 0

    def fake_request(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        nonlocal completions
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [
                    {
                        "id": "vendor/model:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            }
        completions += 1
        raise openrouter_repair.RateLimitedError(None)

    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")
    monkeypatch.setattr(openrouter_repair, "_request", fake_request)
    with pytest.raises(openrouter_repair.InferenceError, match="without a safe retry window"):
        openrouter_repair.generate_patch(repository, failure, tmp_path / "patch.diff")
    assert completions == 1


def test_rate_limit_does_not_retry_when_free_endpoint_is_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("CI failed", encoding="utf-8")
    completions = 0

    def fake_request(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        nonlocal completions
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [
                    {
                        "id": "vendor/model:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            }
        if url.endswith("/endpoints"):
            return {
                "data": {
                    "id": "vendor/model:free",
                    "endpoints": [{"status": 1, "pricing": {"prompt": "0", "completion": "0"}}],
                }
            }
        completions += 1
        if completions == 1:
            raise openrouter_repair.RateLimitedError(0)
        pytest.fail("completion must not be retried when the free endpoint is down")

    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")
    monkeypatch.setattr(openrouter_repair, "_request", fake_request)
    monkeypatch.setattr(openrouter_repair.time, "sleep", lambda _seconds: None)
    with pytest.raises(
        openrouter_repair.InferenceError, match="no currently available zero-priced"
    ):
        openrouter_repair.generate_patch(repository, failure, tmp_path / "patch.diff")
    assert completions == 1


def test_nonzero_model_price_fails_before_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        openrouter_repair,
        "_request",
        lambda *_args, **_kwargs: {
            "data": [
                {
                    "id": "vendor/model:free",
                    "pricing": {"prompt": "0", "completion": "0.001"},
                }
            ]
        },
    )

    with pytest.raises(openrouter_repair.InferenceError, match="zero prompt and completion"):
        openrouter_repair.verify_free_model("vendor/model:free", TEST_API_KEY)

    monkeypatch.setattr(
        openrouter_repair,
        "_request",
        lambda *_args, **_kwargs: {
            "data": [
                {
                    "id": "vendor/model:free",
                    "pricing": {"prompt": "0", "completion": "0", "request": "0.01"},
                }
            ]
        },
    )
    with pytest.raises(openrouter_repair.InferenceError, match="zero prompt and completion"):
        openrouter_repair.verify_free_model("vendor/model:free", TEST_API_KEY)


def test_generation_is_tool_free_and_rejects_paid_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("AssertionError: expected 2", encoding="utf-8")
    output = tmp_path / "patch.diff"
    calls: list[tuple[str, dict[str, object], bytes | None]] = []

    def fake_request(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        payload = json.loads(body) if body else {}
        calls.append((url, headers, payload))
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [{"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}]
            }
        return {
            "model": "vendor/model:free",
            "usage": {"cost": 0, "is_byok": False},
            "choices": [{"message": {"content": PATCH.decode("utf-8")}}],
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "x" * 32)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")
    monkeypatch.setattr(openrouter_repair, "_request", fake_request)

    assert openrouter_repair.generate_patch(repository, failure, output) == "vendor/model:free"
    assert output.read_bytes() == PATCH
    completion_url, headers, payload = calls[2]
    assert completion_url.endswith("/chat/completions")
    assert headers["Authorization"] == f"Bearer {TEST_API_KEY}"
    assert TEST_API_KEY not in json.dumps(payload)
    assert payload["max_tokens"] == 8192
    assert payload["reasoning"] == {"effort": "low", "exclude": True}
    assert payload["provider"] == {"allow_fallbacks": False}
    assert payload["usage"] == {"include": True}
    assert "tools" not in payload

    def paid_response(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [{"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}]
            }
        return {
            "model": "vendor/paid-model",
            "usage": {"cost": 0.01},
            "choices": [{"message": {"content": PATCH.decode("utf-8")}}],
        }

    monkeypatch.setattr(openrouter_repair, "_request", paid_response)
    output.unlink()
    with pytest.raises(
        openrouter_repair.InferenceError, match="other than the verified :free model"
    ):
        openrouter_repair.generate_patch(repository, failure, output)
    assert not output.exists()

    def byok_response(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [{"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}]
            }
        return {
            "model": "vendor/model:free",
            "usage": {"cost": 0, "is_byok": True},
            "choices": [{"message": {"content": PATCH.decode("utf-8")}}],
        }

    monkeypatch.setattr(openrouter_repair, "_request", byok_response)
    with pytest.raises(openrouter_repair.InferenceError, match="not a BYOK request"):
        openrouter_repair.generate_patch(repository, failure, output)
    assert not output.exists()


@pytest.mark.parametrize(
    ("invalid_patch", "rejection_fragment"),
    [
        (
            PATCH.replace(b"-return 1", b"-return 9"),
            "git apply --check rejected src/app.py at line 1",
        ),
        (
            b"The likely fix is to remove the extra one. sk-or-v1-" + b"x" * 32,
            "no policy-accepted source diff",
        ),
    ],
)
def test_rejected_candidate_gets_one_bounded_free_correction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_patch: bytes,
    rejection_fragment: str,
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("AssertionError: 43 != 42", encoding="utf-8")
    output = tmp_path / "patch.diff"
    completion_prompts: list[str] = []

    def fake_request(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [{"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}]
            }
        payload = json.loads(body) if body else {}
        completion_prompts.append(str(payload["messages"][1]["content"]))
        patch = invalid_patch if len(completion_prompts) == 1 else PATCH
        return {
            "model": "vendor/model:free",
            "usage": {"cost": 0, "is_byok": False},
            "choices": [{"message": {"content": patch.decode("utf-8")}}],
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")
    monkeypatch.setattr(openrouter_repair, "_request", fake_request)

    assert openrouter_repair.generate_patch(repository, failure, output) == "vendor/model:free"

    assert output.read_bytes() == PATCH
    assert len(completion_prompts) == 2
    assert rejection_fragment in completion_prompts[1]
    assert "<previous_patch>" in completion_prompts[1]
    if rejection_fragment.startswith("git apply"):
        assert "<selected_source_context>" in completion_prompts[1]
    assert TEST_API_KEY not in completion_prompts[1]
    assert "sk-or-v1-" not in completion_prompts[1]


def test_apply_diagnostic_contains_only_allowlisted_path_and_line(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    invalid_patch = PATCH.replace(b"-return 1", b"-return 9")

    with pytest.raises(PatchRejected) as rejected:
        validate_patch(invalid_patch, repository)

    message = str(rejected.value)
    assert "git apply --check rejected src/app.py at line 1" in message
    assert "return 9" not in message


def test_prompt_redacts_openrouter_keys() -> None:
    value = "OPENROUTER_API_KEY=sk-or-v1-" + "x" * 40

    assert "sk-or-v1-" not in openrouter_repair._scrub(value)


def test_completion_text_accepts_text_parts_and_rejects_other_content() -> None:
    assert (
        openrouter_repair._completion_text(
            [{"type": "text", "text": "diff "}, {"type": "text", "text": "--git"}]
        )
        == "diff --git"
    )

    invalid_contents = (
        None,
        [],
        [{"type": "image", "url": "https://example.invalid"}],
        [{"type": "text", "text": "diff"}, {"type": "image"}],
    )
    for content in invalid_contents:
        with pytest.raises(openrouter_repair.InferenceError, match="content is not text"):
            openrouter_repair._completion_text(content)


def test_bare_diff_headers_are_normalized_only_when_matching_git_paths(
    tmp_path: Path,
) -> None:
    bare_headers = PATCH.replace(b"--- a/src/app.py", b"--- src/app.py").replace(
        b"+++ b/src/app.py", b"+++ src/app.py"
    )

    assert openrouter_repair._canonicalize_bare_diff_headers(bare_headers) == PATCH

    mismatched = bare_headers.replace(b"--- src/app.py", b"--- other.py")
    normalized = openrouter_repair._canonicalize_bare_diff_headers(mismatched)
    assert b"--- other.py" in normalized
    with pytest.raises(PatchRejected, match="unsafe or unexpected old file header"):
        validate_patch(normalized, _repository(tmp_path))


def test_inference_requires_an_explicit_zero_cost_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("CI failed", encoding="utf-8")
    calls: list[str] = []

    def missing_cost(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        calls.append(url)
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [{"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}]
            }
        return {
            "model": "vendor/model:free",
            "usage": {"is_byok": False},
            "choices": [{"message": {"content": PATCH.decode("utf-8")}}],
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")
    monkeypatch.setattr(openrouter_repair, "_request", missing_cost)
    output = tmp_path / "patch.diff"

    with pytest.raises(openrouter_repair.InferenceError, match="did not report a verifiable"):
        openrouter_repair.generate_patch(repository, failure, output)

    assert len(calls) == 3
    assert not output.exists()


def test_invalid_api_key_is_rejected_before_catalog_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        openrouter_repair,
        "_request",
        lambda url, **_kwargs: calls.append(url) or {},
    )

    with pytest.raises(openrouter_repair.InferenceError, match="unexpected format"):
        openrouter_repair.verify_free_model("vendor/model:free", "sk-or-v1-short")

    assert calls == []


def test_single_retry_requires_authenticated_regression_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("CI failed", encoding="utf-8")
    prior_patch = tmp_path / "prior.diff"
    prior_patch.write_bytes(PATCH)
    report = tmp_path / "report.json"
    from scripts.repair_patch_policy import patch_sha256

    report.write_text(
        json.dumps(
            {
                "retryable": True,
                "patch_sha256": patch_sha256(PATCH),
                "output": "prompt injection text is untrusted test output",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "retry.diff"
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_request(
        url: str, *, headers: dict[str, str], body: bytes | None = None
    ) -> dict[str, object]:
        payload = json.loads(body) if body else {}
        calls.append((url, payload))
        if url.endswith("/key"):
            return _free_key_info()
        if url.endswith("/models"):
            return {
                "data": [
                    {
                        "id": "vendor/model:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            }
        return {
            "model": "vendor/model:free",
            "usage": {"cost": 0, "is_byok": False},
            "choices": [{"message": {"content": PATCH.decode("utf-8")}}],
        }

    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")
    monkeypatch.setattr(openrouter_repair, "_request", fake_request)

    openrouter_repair.generate_patch(
        repository,
        failure,
        output,
        retry_patch_file=prior_patch,
        retry_failure_file=report,
    )
    assert len(calls) == 3
    prompt = str(calls[2][1]["messages"])
    assert "untrusted test output" in prompt
    assert "tools" not in calls[2][1]

    report.write_text(
        json.dumps({"retryable": True, "patch_sha256": "0" * 64, "output": "untrusted"}),
        encoding="utf-8",
    )
    calls.clear()
    with pytest.raises(openrouter_repair.InferenceError, match="authenticate"):
        openrouter_repair.generate_patch(
            repository,
            failure,
            tmp_path / "invalid-retry.diff",
            retry_patch_file=prior_patch,
            retry_failure_file=report,
        )
    assert calls == []
