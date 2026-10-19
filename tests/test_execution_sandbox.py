from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from mcp_server.execution_sandbox import (
    MAX_FILE_BYTES,
    MAX_MEMORY_BYTES,
    MAX_PROCESSES,
    _bubblewrap_permission_denied,
    run_untrusted_command,
)


def test_bubblewrap_host_namespace_denial_is_recognized() -> None:
    assert _bubblewrap_permission_denied(
        "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"
    )
    assert not _bubblewrap_permission_denied("bwrap: unknown option --fake")


def test_snapshot_excludes_git_and_common_credential_files(tmp_path: Path) -> None:
    from mcp_server.execution_sandbox import _snapshot_repo

    source = tmp_path / "source"
    snapshot = tmp_path / "snapshot"
    source.mkdir()
    (source / ".git").write_text("gitdir: private", encoding="utf-8")
    (source / ".env").write_text("TOKEN=private", encoding="utf-8")
    (source / ".env.example").write_text("TOKEN=placeholder", encoding="utf-8")
    (source / ".npmrc").write_text("//registry.example/:_authToken=private", encoding="utf-8")
    (source / ".selfheal_venv").mkdir()
    (source / ".selfheal_venv" / "host-tool.exe").write_bytes(b"untrusted tool")
    (source / "secrets").mkdir()
    (source / "secrets" / "local.json").write_text('{"token":"private"}')
    (source / "app.py").write_text("print('ok')", encoding="utf-8")

    _snapshot_repo(source, snapshot)

    assert not (snapshot / ".git").exists()
    assert not (snapshot / ".env").exists()
    assert not (snapshot / ".npmrc").exists()
    assert not (snapshot / ".selfheal_venv").exists()
    assert not (snapshot / "secrets").exists()
    assert (snapshot / ".env.example").is_file()
    assert (snapshot / "app.py").is_file()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="requires Windows AppContainer and Job Objects")
async def test_windows_appcontainer_enforces_boundaries_and_kills_child_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the process-limit probe cheap while proving the Job Object limit is
    # enforced against child processes that stay alive long enough to count.
    process_cap = 4
    monkeypatch.setattr("mcp_server.execution_sandbox.MAX_PROCESSES", process_cap)
    source = tmp_path / "repo"
    source.mkdir()
    script = source / "probe.py"
    outside_secret = tmp_path / "outside-secret.txt"
    outside_marker = tmp_path / "outside-marker.txt"
    outside_secret.write_text("must-not-be-readable", encoding="utf-8")
    trusted_runtime = tmp_path / "trusted-runtime"
    trusted_runtime.mkdir()
    trusted_file = trusted_runtime / "tool.marker"
    trusted_file.write_text("read-only trusted runtime", encoding="utf-8")
    trusted_marker = trusted_runtime / "write-attempt.marker"
    (source / ".env").write_text("TOKEN=repo-secret", encoding="utf-8")
    (source / ".git").write_text("gitdir: host-private-git", encoding="utf-8")
    monkeypatch.setenv("SELFHEAL_SANDBOX_TEST_SECRET", "host-only-secret")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        script.write_text(
            "import json, os, pathlib, socket, subprocess, sys\n"
            "outside = pathlib.Path(sys.argv[1])\n"
            "marker = pathlib.Path(sys.argv[2])\n"
            "trusted_file = pathlib.Path(sys.argv[5])\n"
            "trusted_marker = pathlib.Path(sys.argv[6])\n"
            "checks = {}\n"
            "try: outside.read_text(); checks['filesystem_read_blocked'] = False\n"
            "except OSError: checks['filesystem_read_blocked'] = True\n"
            "try: marker.write_text('escaped'); checks['filesystem_write_blocked'] = False\n"
            "except OSError: checks['filesystem_write_blocked'] = True\n"
            "checks['credential_env_hidden'] = 'SELFHEAL_SANDBOX_TEST_SECRET' not in os.environ\n"
            "checks['credential_file_hidden'] = not pathlib.Path('.env').exists()\n"
            "checks['git_metadata_hidden'] = not pathlib.Path('.git').exists()\n"
            "checks['trusted_runtime_readable'] = trusted_file.read_text() == "
            "'read-only trusted runtime'\n"
            "try: trusted_marker.write_text('escaped'); "
            "checks['trusted_runtime_write_blocked'] = False\n"
            "except OSError: checks['trusted_runtime_write_blocked'] = True\n"
            "try: socket.create_connection(('127.0.0.1', int(sys.argv[3])), timeout=1); "
            "checks['network_blocked'] = False\n"
            "except OSError: checks['network_blocked'] = True\n"
            "pathlib.Path('inside.marker').write_text('only in sandbox copy')\n"
            "children = []\n"
            "for _ in range(100):\n"
            "  try: children.append(subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)\n"
            "  except OSError: break\n"
            "checks['active_processes_limited'] = len(children) < 100 and len(children) < "
            "int(sys.argv[4])\n"
            "checks['child_process_started'] = bool(children)\n"
            "print(json.dumps({'checks': checks, 'child_pids': children}), flush=True)\n"
            "import time; time.sleep(30)\n",
            encoding="utf-8",
        )

        result = await run_untrusted_command(
            [
                sys.executable,
                str(script),
                str(outside_secret),
                str(outside_marker),
                str(port),
                str(process_cap),
                str(trusted_file),
                str(trusted_marker),
            ],
            cwd=source,
            timeout=4,
            read_only_paths=[trusted_runtime],
        )

    assert result.get("sandbox") == "windows-appcontainer-job", result
    assert result["timed_out"] is True, result
    assert not outside_marker.exists()
    payload = json.loads(result["output"].splitlines()[0])
    assert all(payload["checks"].values()), {
        **payload["checks"],
        "child_process_count": len(payload["child_pids"]),
    }
    assert len(payload["child_pids"]) < process_cap, payload["child_pids"]
    assert (source / ".env").is_file()  # source copy remained unchanged
    assert (source / ".git").is_file()
    assert "inside.marker" not in {path.name for path in source.iterdir()}
    assert all(not psutil.pid_exists(pid) for pid in payload["child_pids"])


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="requires Windows AppContainer and Job Objects")
@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="requires a virtual-environment Python")
async def test_windows_appcontainer_can_launch_the_active_venv_python(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()

    result = await run_untrusted_command(
        [sys.executable, "-c", "print('venv runtime is readable')"], cwd=source, timeout=10
    )

    assert result.get("sandbox") == "windows-appcontainer-job", result
    assert result["passed"] is True, result
    assert "venv runtime is readable" in result["output"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="requires Windows AppContainer and Node.js")
async def test_windows_appcontainer_runs_npm_with_staged_node_runtime(tmp_path: Path) -> None:
    if not shutil.which("node") or not shutil.which("npm"):
        pytest.skip("Node.js and npm are required for this Windows integration test")
    source = tmp_path / "node-project"
    source.mkdir()
    (source / "package.json").write_text(
        '{"private":true,"scripts":{"test":"node test.js"}}\n', encoding="utf-8"
    )
    (source / "test.js").write_text(
        "if (process.version.length < 2) process.exit(1); "
        "process.stdout.write('npm sandbox test passed\\n');\n",
        encoding="utf-8",
    )

    result = await run_untrusted_command("npm test", cwd=source, timeout=45)

    assert result.get("sandbox") == "windows-appcontainer-job", result
    assert result["passed"] is True, result
    assert "npm sandbox test passed" in result["output"]
    assert not (source / "node_modules").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux Bubblewrap")
async def test_linux_bubblewrap_enforces_boundaries_and_resource_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0 or not shutil.which("bwrap"):
        pytest.skip("Bubblewrap requires a non-root worker and an installed bwrap binary")

    probe = await asyncio.to_thread(
        subprocess.run,
        [
            "bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--dir",
            "/etc",
            "--dir",
            "/usr",
            "--dir",
            "/lib",
            "--dir",
            "/lib64",
            "--ro-bind",
            "/etc/ld.so.cache",
            "/etc/ld.so.cache",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--",
            "/usr/bin/true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode:
        details = (probe.stderr or probe.stdout).strip()[-500:]
        if "operation not permitted" in details.lower() or "permission denied" in details.lower():
            pytest.skip(f"Bubblewrap namespaces unavailable in this runner: {details}")
        pytest.fail(f"Bubblewrap preflight failed unexpectedly: {details}")

    source = tmp_path / "repo"
    source.mkdir()
    (source / ".env").write_text("TOKEN=repo-secret", encoding="utf-8")
    script = source / "probe.py"
    outside_secret = tmp_path / "outside-secret.txt"
    outside_marker = tmp_path / "outside-marker.txt"
    child_marker = f"selfheal-bwrap-child-{tmp_path.name}"
    outside_secret.write_text("host-only-secret", encoding="utf-8")
    monkeypatch.setenv("SELFHEAL_SANDBOX_TEST_SECRET", "host-only-secret")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        script.write_text(
            "import json, os, pathlib, resource, socket, subprocess, sys, time\n"
            "checks = {}\n"
            "try:\n"
            "    pathlib.Path(sys.argv[1]).read_text()\n"
            "    checks['filesystem_read_blocked'] = False\n"
            "except OSError:\n"
            "    checks['filesystem_read_blocked'] = True\n"
            "try:\n"
            "    pathlib.Path(sys.argv[2]).write_text('escaped')\n"
            "    checks['filesystem_write_blocked'] = False\n"
            "except OSError:\n"
            "    checks['filesystem_write_blocked'] = True\n"
            "checks['credential_env_hidden'] = 'SELFHEAL_SANDBOX_TEST_SECRET' not in os.environ\n"
            "checks['credential_file_hidden'] = not pathlib.Path('.env').exists()\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', int(sys.argv[3])), timeout=1)\n"
            "    checks['network_blocked'] = False\n"
            "except OSError:\n"
            "    checks['network_blocked'] = True\n"
            "limits = {name: resource.getrlimit(value)[0] for name, value in {\n"
            "  'cpu': resource.RLIMIT_CPU, 'memory': resource.RLIMIT_AS,\n"
            "  'file': resource.RLIMIT_FSIZE, 'processes': resource.RLIMIT_NPROC}.items()}\n"
            "checks['resource_limits_set'] = (\n"
            "    limits['cpu'] <= int(sys.argv[4])\n"
            "    and limits['memory'] <= int(sys.argv[5])\n"
            "    and limits['file'] <= int(sys.argv[6])\n"
            "    and limits['processes'] <= int(sys.argv[7])\n"
            ")\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(30)', sys.argv[8]]\n"
            ")\n"
            "payload = {\n"
            "    'checks': checks, 'limits': limits,\n"
            "    'child_marker': sys.argv[8], 'child_pid': child.pid,\n"
            "}\n"
            "print(json.dumps(payload), flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )

        result = await run_untrusted_command(
            [
                sys.executable,
                script.name,
                str(outside_secret),
                str(outside_marker),
                str(port),
                "4",
                str(MAX_MEMORY_BYTES),
                str(MAX_FILE_BYTES),
                str(MAX_PROCESSES),
                child_marker,
            ],
            cwd=source,
            timeout=4,
        )

    assert result.get("sandbox") == "bubblewrap", result
    assert result["timed_out"] is True, result
    assert not outside_marker.exists()
    payload = json.loads(result["output"].splitlines()[0])
    assert all(payload["checks"].values()), payload
    assert payload["child_marker"] == child_marker
    assert not any(
        child_marker in " ".join(process.info.get("cmdline") or [])
        for process in psutil.process_iter(["cmdline"])
    )
