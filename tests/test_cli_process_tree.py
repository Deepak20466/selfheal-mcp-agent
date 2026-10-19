"""Provider CLI timeout cleanup must terminate descendant processes too."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from healer import cli_common


async def test_kill_process_tree_signals_the_posix_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(cli_common.sys, "platform", "linux")
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        cli_common.os,
        "killpg",
        lambda pid, signum: signals.append((pid, signum)),
        raising=False,
    )

    await cli_common.kill_process_tree(1234)

    assert signals == [(1234, signal.SIGKILL)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group integration test")
async def test_kill_process_tree_stops_cli_descendants(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-survived"
    child_code = "import pathlib,sys,time; time.sleep(1); pathlib.Path(sys.argv[1]).touch()"
    parent_code = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
        "print('ready', flush=True); time.sleep(30)"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        child_code,
        str(marker),
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert await asyncio.wait_for(process.stdout.readline(), timeout=5) == b"ready\n"

    await cli_common.kill_process_tree(process.pid)
    await asyncio.wait_for(process.wait(), timeout=5)
    await asyncio.sleep(1.2)

    assert not marker.exists(), "a CLI descendant survived process-group timeout cleanup"
