"""Positive/negative probes for the GitHub-hosted repair test boundary."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server.execution_sandbox import run_untrusted_command  # noqa: E402


async def verify() -> None:
    with tempfile.TemporaryDirectory(prefix="selfheal-repair-boundary-") as temporary:
        root = Path(temporary)
        repository = root / "repository"
        repository.mkdir()
        outside = root / "outside-secret.txt"
        outside.write_text("host-only-canary", encoding="utf-8")
        (repository / "tests").mkdir()
        (repository / ".env").write_text("PRIVATE_CANARY=must-not-be-copied", encoding="utf-8")
        probe = repository / "tests" / "probe.py"
        probe.write_text(
            "import os, socket, sys\n"
            "from pathlib import Path\n"
            "assert os.environ.get('SELFHEAL_TEST_SECRET') is None\n"
            "assert os.environ.get('OPENROUTER_API_KEY') is None\n"
            "assert os.environ.get('GITHUB_TOKEN') is None\n"
            "assert os.environ.get('GH_TOKEN') is None\n"
            "assert not Path('.env').exists()\n"
            "try:\n"
            "    Path('tests/probe.py').write_text('tampered')\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('sandbox tests directory was writable')\n"
            "try:\n"
            "    Path(sys.argv[1]).read_text()\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('sandbox read a file outside its workspace')\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), timeout=1)\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('sandbox network namespace allowed external egress')\n"
            "try:\n"
            "    socket.create_connection(('example.com', 443), timeout=2)\n"
            "except OSError:\n"
            "    pass\n"
            "else:\n"
            "    raise AssertionError('sandbox allowed DNS-backed external egress')\n"
            "print('offline, secret-free sandbox probes passed')\n",
            encoding="utf-8",
        )
        canaries = {
            "SELFHEAL_TEST_SECRET": "parent-only-canary",
            "OPENROUTER_API_KEY": "sk-or-v1-" + "x" * 32,
            "GITHUB_TOKEN": "github-write-token-canary",
            "GH_TOKEN": "github-read-token-canary",
        }
        previous = {name: os.environ.get(name) for name in canaries}
        try:
            os.environ.update(canaries)
            result = await run_untrusted_command(
                [sys.executable, "tests/probe.py", str(outside)],
                cwd=repository,
                timeout=10,
                read_only_workspace_paths=("tests",),
            )
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        if result.get("sandbox_unavailable") or not result.get("passed"):
            raise RuntimeError(f"repair sandbox probe failed closed: {result}")
        print(str(result.get("output", "")))


if __name__ == "__main__":
    asyncio.run(verify())
