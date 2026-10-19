"""Provider command-line agents must fail closed without OS containment."""

from pathlib import Path

import pytest

from healer import agent_codex, agent_free, agent_gemini
from healer.cli_common import CLISandboxUnavailableError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "provider"),
    [
        (agent_codex.run_codex_cli, "Codex"),
        (agent_free.run_claude_cli, "Claude Code"),
        (agent_gemini.run_gemini_cli, "Gemini"),
    ],
)
async def test_provider_cli_fails_closed_before_starting_process(
    runner, provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def process_must_not_start(*_args, **_kwargs):
        pytest.fail("provider CLI process must not start without OS containment")

    monkeypatch.setattr(agent_free.asyncio, "create_subprocess_exec", process_must_not_start)
    monkeypatch.setattr(agent_codex.asyncio, "create_subprocess_exec", process_must_not_start)
    monkeypatch.setattr(agent_gemini.asyncio, "create_subprocess_exec", process_must_not_start)

    with pytest.raises(CLISandboxUnavailableError, match="no verified OS sandbox"):
        await runner("untrusted prompt", cwd=tmp_path)
