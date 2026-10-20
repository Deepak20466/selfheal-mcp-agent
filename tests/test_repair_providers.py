"""Provider-neutral repair contract and free-tier adapter tests."""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import openrouter_repair, repair_providers
from scripts.repair_providers import (
    FreeQuotaUnavailable,
    GeminiModelTemporarilyUnavailable,
    GeminiProvider,
    GroqProvider,
    ProviderCompletion,
    ProviderError,
)

PATCH = b"""diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-return 1
+return 2
"""
GEMINI_KEY = "test-gemini-api-key"
GROQ_KEY = "test-groq-api-key"


def _fresh_quota_check() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _response(patch: bytes = PATCH, diagnosis: str = "The return value is incorrect.") -> str:
    return json.dumps({"diagnosis": diagnosis, "patch": patch.decode()})


def _gemini_payload(prompt: str, model: str = "gemini-2.5-pro") -> dict[str, Any]:
    return {
        "modelVersion": model,
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": prompt}]},
            }
        ],
    }


def _gemini_model_list(*names: str) -> dict[str, Any]:
    return {
        "models": [
            {"name": f"models/{name}", "supportedGenerationMethods": ["generateContent"]}
            for name in names
        ]
    }


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("return 1\n", encoding="utf-8")
    return root


def test_gemini_uses_pro_first_with_strict_json_and_no_tools() -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any] | None]] = []
    output = _response()

    def request_json(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        provider: str,
    ) -> dict[str, Any]:
        payload = json.loads(body) if body else None
        calls.append((url, headers, payload))
        if url.endswith("/models?pageSize=100"):
            return _gemini_model_list("gemini-2.5-pro", "gemini-2.5-flash")
        return _gemini_payload(output)

    client = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="true",
        flash_quota_confirmed="true",
        quota_checked_utc=_fresh_quota_check(),
        request_json=request_json,
    )

    completion = client.complete("untrusted repository prompt")

    assert completion == ProviderCompletion("gemini", "gemini-2.5-pro", output)
    assert len(calls) == 2
    assert calls[0][0] == "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100"
    assert calls[0][1]["x-goog-api-key"] == GEMINI_KEY
    assert GEMINI_KEY not in calls[0][0]
    assert calls[1][0].endswith("/models/gemini-2.5-pro:generateContent")
    generation = calls[1][2]["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["responseSchema"]["required"] == ["diagnosis", "patch"]
    assert "tools" not in calls[1][2]


def test_gemini_uses_flash_when_pro_quota_is_exhausted() -> None:
    selected: list[str] = []
    output = _response()

    def request_json(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        provider: str,
    ) -> dict[str, Any]:
        if url.endswith("/models?pageSize=100"):
            return _gemini_model_list("gemini-2.5-pro", "gemini-2.5-flash")
        model = "gemini-2.5-pro" if "pro:generateContent" in url else "gemini-2.5-flash"
        selected.append(model)
        if model == "gemini-2.5-pro":
            raise FreeQuotaUnavailable("test quota exhausted")
        response = _gemini_payload(output)
        response["modelVersion"] = "gemini-2.5-flash"
        return response

    client = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="true",
        flash_quota_confirmed="true",
        quota_checked_utc=_fresh_quota_check(),
        request_json=request_json,
    )

    result = client.complete("prompt")

    assert result.model == "gemini-2.5-flash"
    assert selected == ["gemini-2.5-pro", "gemini-2.5-flash"]


def test_gemini_prefers_confirmed_free_38_flash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_38_FLASH_FREE_QUOTA", "true")
    output = _response()
    selected: list[str] = []

    def request_json(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        provider: str,
    ) -> dict[str, Any]:
        if url.endswith("/models?pageSize=100"):
            return _gemini_model_list("gemini-3.8-flash", "gemini-2.5-pro", "gemini-2.5-flash")
        selected.append(url.split("/models/", 1)[1].split(":", 1)[0])
        return _gemini_payload(output, "gemini-3.8-flash")

    client = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="false",
        flash_quota_confirmed="true",
        quota_checked_utc=_fresh_quota_check(),
        request_json=request_json,
    )

    result = client.complete("prompt")

    assert result.model == "gemini-3.8-flash"
    assert selected == ["gemini-3.8-flash"]


def test_gemini_skips_404_model_and_uses_next_confirmed_free_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_38_FLASH_FREE_QUOTA", "true")
    monkeypatch.setenv("GEMINI_35_FLASH_LITE_FREE_QUOTA", "true")
    output = _response()
    selected: list[str] = []

    def request_json(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        provider: str,
    ) -> dict[str, Any]:
        if url.endswith("/models?pageSize=100"):
            return _gemini_model_list("gemini-3.8-flash", "gemini-3.5-flash-lite")
        model = url.split("/models/", 1)[1].split(":", 1)[0]
        selected.append(model)
        if model == "gemini-3.8-flash":
            raise FreeQuotaUnavailable("Gemini model is unavailable to this project")
        return _gemini_payload(output, model)

    client = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="false",
        flash_quota_confirmed="false",
        quota_checked_utc=_fresh_quota_check(),
        request_json=request_json,
    )

    result = client.complete("prompt")

    assert result.model == "gemini-3.5-flash-lite"
    assert selected == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]


def test_gemini_falls_back_to_next_confirmed_model_on_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_38_FLASH_FREE_QUOTA", "true")
    monkeypatch.setenv("GEMINI_35_FLASH_LITE_FREE_QUOTA", "true")
    output = _response()
    selected: list[str] = []

    def request_json(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        provider: str,
    ) -> dict[str, Any]:
        if url.endswith("/models?pageSize=100"):
            return _gemini_model_list("gemini-3.8-flash", "gemini-3.5-flash-lite")
        model = url.split("/models/", 1)[1].split(":", 1)[0]
        selected.append(model)
        if model == "gemini-3.8-flash":
            raise GeminiModelTemporarilyUnavailable("Gemini model is temporarily unavailable")
        return _gemini_payload(output, model)

    client = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="false",
        flash_quota_confirmed="false",
        quota_checked_utc=_fresh_quota_check(),
        request_json=request_json,
    )

    result = client.complete("prompt")

    assert result.model == "gemini-3.5-flash-lite"
    assert selected == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]


def test_gemini_404_is_model_unavailability_not_a_paid_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotFoundOpener:
        def open(self, *_args: Any, **_kwargs: Any) -> Any:
            raise urllib.error.HTTPError("https://example.invalid", 404, "Not Found", None, None)

    monkeypatch.setattr(
        repair_providers.urllib.request,
        "build_opener",
        lambda *_args: NotFoundOpener(),
    )

    with pytest.raises(FreeQuotaUnavailable, match="model is unavailable"):
        repair_providers._request_json(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            body=b"{}",
            provider="Gemini",
        )


def test_gemini_503_is_temporary_model_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableOpener:
        def open(self, *_args: Any, **_kwargs: Any) -> Any:
            raise urllib.error.HTTPError(
                "https://example.invalid", 503, "Service Unavailable", None, None
            )

    monkeypatch.setattr(
        repair_providers.urllib.request,
        "build_opener",
        lambda *_args: UnavailableOpener(),
    )

    with pytest.raises(GeminiModelTemporarilyUnavailable, match="temporarily unavailable"):
        repair_providers._request_json(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            body=b"{}",
            provider="Gemini",
        )


def test_gemini_request_timeout_is_longer_and_other_provider_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[int] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b"{}"

    class CapturingOpener:
        def open(self, *_args: Any, timeout: int) -> Response:
            timeouts.append(timeout)
            return Response()

    monkeypatch.setattr(
        repair_providers.urllib.request,
        "build_opener",
        lambda *_args: CapturingOpener(),
    )

    repair_providers._request_json("https://example.invalid", headers={}, provider="Gemini")
    repair_providers._request_json("https://example.invalid", headers={}, provider="Groq")

    assert timeouts == [120, 45]


def test_gemini_requires_free_project_and_positive_model_quota_before_http() -> None:
    calls = 0

    def request_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    no_project_confirmation = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="false",
        pro_quota_confirmed="true",
        request_json=request_json,
    )
    with pytest.raises(ProviderError, match="billing disabled"):
        no_project_confirmation.complete("prompt")

    no_quota_confirmation = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="false",
        flash_quota_confirmed="false",
        quota_checked_utc=_fresh_quota_check(),
        request_json=request_json,
    )
    with pytest.raises(
        FreeQuotaUnavailable, match="no supported Gemini model has confirmed free quota"
    ):
        no_quota_confirmation.complete("prompt")
    assert calls == 0


def test_gemini_does_not_call_a_model_missing_from_current_catalog() -> None:
    calls: list[str] = []

    def request_json(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        provider: str,
    ) -> dict[str, Any]:
        calls.append(url)
        return _gemini_model_list("gemini-2.5-flash")

    client = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="true",
        flash_quota_confirmed="false",
        quota_checked_utc=_fresh_quota_check(),
        request_json=request_json,
    )
    with pytest.raises(FreeQuotaUnavailable, match="currently available"):
        client.complete("prompt")
    assert len(calls) == 1
    assert calls[0].endswith("/models?pageSize=100")


@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        (datetime.now(UTC) - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (datetime.now(UTC) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "not-a-timestamp",
    ],
)
def test_gemini_rejects_missing_stale_or_invalid_quota_check_before_network(
    timestamp: str | None,
) -> None:
    calls = 0

    def request_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    client = GeminiProvider(
        GEMINI_KEY,
        project_free_tier_confirmed="true",
        pro_quota_confirmed="true",
        quota_checked_utc=timestamp,
        request_json=request_json,
    )
    with pytest.raises(ProviderError, match="quota check"):
        client.complete("prompt")
    assert calls == 0


def test_groq_requires_free_plan_attestation_before_model_list_or_inference() -> None:
    calls = 0

    def request_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {}

    client = GroqProvider(
        GROQ_KEY,
        free_plan_confirmed="false",
        quota_confirmed="true",
        request_json=request_json,
    )
    with pytest.raises(FreeQuotaUnavailable, match="disabled without verified Free Plan"):
        client.complete("prompt")
    assert calls == 0


def test_groq_checks_model_then_requests_schema_on_on_demand_tier() -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    output = _response()

    def request_json(
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        provider: str,
    ) -> dict[str, Any]:
        payload = json.loads(body) if body else None
        calls.append((url, payload))
        if url.endswith("/models"):
            return {"data": [{"id": "openai/gpt-oss-120b"}]}
        return {
            "model": "openai/gpt-oss-120b",
            "choices": [{"finish_reason": "stop", "message": {"content": output}}],
        }

    client = GroqProvider(
        GROQ_KEY,
        free_plan_confirmed="true",
        quota_confirmed="true",
        request_json=request_json,
    )
    result = client.complete("prompt")
    assert result == ProviderCompletion("groq", "openai/gpt-oss-120b", output)
    assert calls[0][0].endswith("/models")
    request = calls[1][1]
    assert request is not None
    assert request["service_tier"] == "on_demand"
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["tools"] == []


def test_provider_chain_requires_gemini_and_keeps_groq_and_openrouter_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "x" * 32)
    monkeypatch.setenv("OPENROUTER_FREE_MODEL", "vendor/model:free")

    chain = openrouter_repair._provider_chain()
    assert [provider.name for provider in chain] == ["gemini", "groq", "openrouter"]

    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(openrouter_repair.InferenceError, match="GEMINI_API_KEY is required"):
        openrouter_repair._provider_chain()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"diagnosis":"x","patch":"x","extra":"field"}',
        '{"diagnosis":"x","diagnosis":"y","patch":"x"}',
        '{"diagnosis":4,"patch":"x"}',
        '{"diagnosis":"x","patch":"\\ud800"}',
    ],
)
def test_common_response_parser_rejects_malformed_or_nonexact_json(content: str) -> None:
    with pytest.raises(openrouter_repair.InferenceError, match="strict JSON schema"):
        openrouter_repair._parse_repair_response(content)


def test_common_controller_corrects_stale_patch_with_exact_source_and_records_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("AssertionError: expected 2", encoding="utf-8")
    stale = PATCH.replace(b"-return 1", b"-return 9")
    prompts: list[str] = []

    class StubProvider:
        name = "gemini"

        def complete(self, prompt: str) -> ProviderCompletion:
            prompts.append(prompt)
            patch = stale if len(prompts) == 1 else PATCH
            return ProviderCompletion("gemini", "gemini-2.5-pro", _response(patch))

    monkeypatch.setattr(openrouter_repair, "_provider_chain", lambda: [StubProvider()])
    output = tmp_path / "patch.diff"
    diagnosis_file = tmp_path / "diagnosis.json"
    result = openrouter_repair.generate_patch(
        repository,
        failure,
        output,
        output_diagnosis_file=diagnosis_file,
    )
    assert result == "gemini-2.5-pro"
    assert output.read_bytes() == PATCH
    assert len(prompts) == 2
    assert "git apply --check rejected src/app.py at line 1" in prompts[1]
    assert "<selected_source_context>" in prompts[1]
    assert "1: return 1" in prompts[1]
    evidence = json.loads(diagnosis_file.read_text(encoding="utf-8"))
    assert evidence["provider"] == "gemini"
    assert evidence["model"] == "gemini-2.5-pro"
    assert evidence["diagnosis"] == "The return value is incorrect."


def test_common_controller_corrects_malformed_response_once_and_then_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("CI failed", encoding="utf-8")
    prompts: list[str] = []

    class StubProvider:
        name = "gemini"

        def complete(self, prompt: str) -> ProviderCompletion:
            prompts.append(prompt)
            response = "not json" if len(prompts) == 1 else _response()
            return ProviderCompletion("gemini", "gemini-2.5-pro", response)

    monkeypatch.setattr(openrouter_repair, "_provider_chain", lambda: [StubProvider()])
    output = tmp_path / "patch.diff"
    openrouter_repair.generate_patch(repository, failure, output)
    assert output.read_bytes() == PATCH
    assert len(prompts) == 2
    assert "did not match the exact JSON schema" in prompts[1]


def test_common_controller_does_not_correct_a_security_policy_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    failure = tmp_path / "failure.txt"
    failure.write_text("CI failed", encoding="utf-8")
    unsafe = PATCH.replace(b"src/app.py", b"tests/test_app.py")
    calls = 0

    class StubProvider:
        name = "gemini"

        def complete(self, _prompt: str) -> ProviderCompletion:
            nonlocal calls
            calls += 1
            return ProviderCompletion("gemini", "gemini-2.5-pro", _response(unsafe))

    monkeypatch.setattr(openrouter_repair, "_provider_chain", lambda: [StubProvider()])
    with pytest.raises(openrouter_repair.InferenceError, match="protected directory"):
        openrouter_repair.generate_patch(repository, failure, tmp_path / "patch.diff")
    assert calls == 1
