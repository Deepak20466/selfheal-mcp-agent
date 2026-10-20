"""Provider adapters for the provider-neutral, source-only repair controller.

Adapters only send bounded text to inference APIs. They never receive tools,
never execute target code, and require an explicit free-tier eligibility gate
before making a completion request.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
GROQ_API_ROOT = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-120b"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_QUOTA_CONFIRMATION_AGE_SECONDS = 15 * 60

REPAIR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "diagnosis": {
            "type": "string",
            "description": "One concise diagnosis grounded in the supplied failure.",
        },
        "patch": {
            "type": "string",
            "description": "One unified git diff, or an empty string if no safe fix is clear.",
        },
    },
    "required": ["diagnosis", "patch"],
    "additionalProperties": False,
}


class ProviderError(RuntimeError):
    """A provider request or response failed closed."""


class FreeQuotaUnavailable(ProviderError):
    """A provider has no usable verified free quota for this request."""


@dataclass(frozen=True)
class ProviderCompletion:
    provider: str
    model: str
    content: str


class RepairProvider(Protocol):
    """Common adapter contract used by the repair controller."""

    name: str

    def complete(self, prompt: str) -> ProviderCompletion: ...


JsonRequest = Callable[..., dict[str, Any]]


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
    provider: str,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=body, headers=headers, method="POST" if body is not None else "GET"
    )

    class RejectRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, response_headers: Any, newurl: str
        ) -> None:
            raise ProviderError(f"{provider} endpoint attempted to redirect the request")

    try:
        with urllib.request.build_opener(RejectRedirect).open(request, timeout=45) as response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise FreeQuotaUnavailable(f"{provider} free quota is unavailable") from None
        raise ProviderError(f"{provider} returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"{provider} request failed ({type(exc).__name__})") from None
    if len(content) > MAX_RESPONSE_BYTES:
        raise ProviderError(f"{provider} response exceeded the size limit")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError(f"{provider} returned invalid JSON") from None
    if not isinstance(value, dict):
        raise ProviderError(f"{provider} response must be a JSON object")
    return value


def _api_key(value: str | None, provider: str) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ProviderError(f"{provider} credential is missing or malformed")
    return value


def _is_true(value: str | None) -> bool:
    return value == "true"


class GeminiProvider:
    """Direct Gemini API adapter with explicit project and model quota gates.

    Google's API does not expose account-specific remaining free quota to an
    API-key-only caller. The two model quota flags therefore represent a
    just-checked AI Studio quota view; the project flag attests the key belongs
    to a project with billing disabled and no purchased credits. The adapter
    also verifies current model availability and supported generation methods
    before sending a completion.
    """

    name = "gemini"
    _MODELS = (
        ("gemini-2.5-pro", "GEMINI_25_PRO_FREE_QUOTA"),
        ("gemini-2.5-flash", "GEMINI_25_FLASH_FREE_QUOTA"),
    )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project_free_tier_confirmed: str | None = None,
        pro_quota_confirmed: str | None = None,
        flash_quota_confirmed: str | None = None,
        quota_checked_utc: str | None = None,
        request_json: JsonRequest | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
        self.project_free_tier_confirmed = (
            project_free_tier_confirmed
            if project_free_tier_confirmed is not None
            else os.environ.get("GEMINI_FREE_TIER_CONFIRMED")
        )
        self.quota_confirmed = {
            "gemini-2.5-pro": (
                pro_quota_confirmed
                if pro_quota_confirmed is not None
                else os.environ.get("GEMINI_25_PRO_FREE_QUOTA")
            ),
            "gemini-2.5-flash": (
                flash_quota_confirmed
                if flash_quota_confirmed is not None
                else os.environ.get("GEMINI_25_FLASH_FREE_QUOTA")
            ),
        }
        self.quota_checked_utc = (
            quota_checked_utc
            if quota_checked_utc is not None
            else os.environ.get("GEMINI_QUOTA_CHECKED_UTC")
        )
        self._request_json = request_json or _request_json

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-goog-api-key": _api_key(self.api_key, "Gemini"),
        }

    def _eligible_models(self) -> list[str]:
        if not _is_true(self.project_free_tier_confirmed):
            raise ProviderError(
                "Gemini is blocked until its API project is verified as Free Tier "
                "with billing disabled and no purchased credits"
            )
        raw_checked_at = self.quota_checked_utc
        try:
            if not isinstance(raw_checked_at, str) or not raw_checked_at.endswith("Z"):
                raise ValueError("UTC marker is required")
            checked_at = datetime.strptime(raw_checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            raise ProviderError(
                "Gemini Free Tier/quota check is missing or not a valid UTC timestamp"
            ) from None
        age_seconds = (datetime.now(UTC) - checked_at).total_seconds()
        if age_seconds < -60 or age_seconds > MAX_QUOTA_CONFIRMATION_AGE_SECONDS:
            raise ProviderError(
                "Gemini Free Tier/quota check is older than 15 minutes or from the future"
            )
        requested = [model for model, flag in self._MODELS if _is_true(self.quota_confirmed[model])]
        if not requested:
            raise FreeQuotaUnavailable("Gemini 2.5 Pro and Flash have no confirmed free quota")

        key = _api_key(self.api_key, "Gemini")
        del key  # Validate before the read-only models request; never put it in a URL.
        data = self._request_json(
            f"{GEMINI_API_ROOT}/models?pageSize=100",
            headers=self._headers(),
            provider="Gemini",
        )
        models = data.get("models")
        if not isinstance(models, list):
            raise ProviderError("Gemini model catalog has an unexpected shape")
        available: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name")
            methods = item.get("supportedGenerationMethods")
            if not isinstance(raw_name, str) or not isinstance(methods, list):
                continue
            model_name = raw_name.removeprefix("models/")
            if "generateContent" in methods:
                available.add(model_name)
        return [model for model in requested if model in available]

    def _generate(self, model: str, prompt: str) -> ProviderCompletion:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                # Gemini supports a JSON Schema subset. Extra keys are still
                # rejected by the common local parser after the response.
                "responseSchema": {
                    key: value
                    for key, value in REPAIR_JSON_SCHEMA.items()
                    if key != "additionalProperties"
                },
                "maxOutputTokens": 8192,
                "temperature": 0.1,
            },
        }
        response = self._request_json(
            f"{GEMINI_API_ROOT}/models/{quote(model, safe='-')}:generateContent",
            headers=self._headers(),
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            provider="Gemini",
        )
        returned_model = response.get("modelVersion")
        if not isinstance(returned_model, str) or not returned_model.startswith(model):
            raise ProviderError("Gemini returned an unexpected model version")
        candidates = response.get("candidates")
        if (
            not isinstance(candidates, list)
            or not candidates
            or not isinstance(candidates[0], dict)
        ):
            raise ProviderError("Gemini returned no completion candidate")
        candidate = candidates[0]
        if candidate.get("finishReason") != "STOP":
            raise ProviderError("Gemini completion did not finish normally")
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts:
            raise ProviderError("Gemini completion content is missing")
        if any(
            not isinstance(part, dict) or not isinstance(part.get("text"), str)
            for part in parts
            if not isinstance(part, dict) or not part.get("thought", False)
        ):
            raise ProviderError("Gemini completion content is not text")
        text = "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict) and not part.get("thought", False)
        )
        if not text:
            raise ProviderError("Gemini completion content is empty")
        return ProviderCompletion(self.name, model, text)

    def complete(self, prompt: str) -> ProviderCompletion:
        models = self._eligible_models()
        if not models:
            raise FreeQuotaUnavailable("no confirmed free Gemini model is currently available")
        unavailable: FreeQuotaUnavailable | None = None
        for model in models:
            try:
                return self._generate(model, prompt)
            except FreeQuotaUnavailable as exc:
                unavailable = exc
        raise unavailable or FreeQuotaUnavailable("Gemini free quota is unavailable")


class GroqProvider:
    """Optional GPT-OSS adapter, disabled until its free account is attested."""

    name = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        free_plan_confirmed: str | None = None,
        quota_confirmed: str | None = None,
        request_json: JsonRequest | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY")
        self.free_plan_confirmed = (
            free_plan_confirmed
            if free_plan_confirmed is not None
            else os.environ.get("GROQ_FREE_PLAN_CONFIRMED")
        )
        self.quota_confirmed = (
            quota_confirmed
            if quota_confirmed is not None
            else os.environ.get("GROQ_GPT_OSS_120B_FREE_QUOTA")
        )
        self._request_json = request_json or _request_json

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {_api_key(self.api_key, 'Groq')}",
            "Content-Type": "application/json",
        }

    def complete(self, prompt: str) -> ProviderCompletion:
        if not _is_true(self.free_plan_confirmed):
            raise FreeQuotaUnavailable("Groq backup is disabled without verified Free Plan status")
        if not _is_true(self.quota_confirmed):
            raise FreeQuotaUnavailable("Groq GPT-OSS 120B has no confirmed free quota")
        self._headers()  # Validate the credential before any network request.
        catalog = self._request_json(
            f"{GROQ_API_ROOT}/models", headers=self._headers(), provider="Groq"
        )
        items = catalog.get("data")
        if not isinstance(items, list) or not any(
            isinstance(item, dict) and item.get("id") == GROQ_MODEL for item in items
        ):
            raise FreeQuotaUnavailable("Groq GPT-OSS 120B is not currently available")
        payload = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": 8192,
            "temperature": 0.1,
            "reasoning_effort": "low",
            "service_tier": "on_demand",
            "tools": [],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "selfheal_repair",
                    "strict": True,
                    "schema": REPAIR_JSON_SCHEMA,
                },
            },
        }
        response = self._request_json(
            f"{GROQ_API_ROOT}/chat/completions",
            headers=self._headers(),
            body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            provider="Groq",
        )
        if response.get("model") != GROQ_MODEL:
            raise ProviderError("Groq returned an unexpected model")
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("Groq returned no completion choice")
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if choice.get("finish_reason") != "stop" or not isinstance(content, str):
            raise ProviderError("Groq completion was incomplete or non-text")
        return ProviderCompletion(self.name, GROQ_MODEL, content)
