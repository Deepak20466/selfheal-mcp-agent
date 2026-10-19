"""OS-enforced execution boundary for repository-controlled commands.

This module deliberately has no native-process fallback. Linux requires
Bubblewrap namespaces; Windows uses an AppContainer process in a resource
limited Job Object. Unsupported operating systems or missing OS facilities
raise :class:`SandboxUnavailableError` before repository code is started.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sentinel.scrubber import scrub_text

MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_FILES = 100_000
MAX_COMMAND_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_ADDITIONAL_WORKSPACE_BYTES = 256 * 1024 * 1024
MAX_MEMORY_BYTES = 1024 * 1024 * 1024
MAX_PROCESSES = 64
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_OPEN_FILES = 128

_SECRET_FILES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".git-credentials",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "secret.json",
        "secrets.json",
        "token.json",
        "service-account.json",
    }
)
_SECRET_DIRS = frozenset({".aws", ".ssh", ".gnupg", ".kube", ".docker", "secrets", "credentials"})
_SECRET_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
_REPARSE_POINT = 0x400


class SandboxUnavailableError(RuntimeError):
    """Raised when the host cannot provide the requested OS sandbox."""


def _bubblewrap_permission_denied(output: str) -> bool:
    """Identify Bubblewrap startup failures caused by host namespace policy."""
    return bool(
        re.search(
            r"(?im)^bwrap:.*(?:operation not permitted|permission denied)",
            output,
        )
    )


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float
    memory_bytes: int = MAX_MEMORY_BYTES
    max_processes: int = MAX_PROCESSES
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES
    max_additional_workspace_bytes: int = MAX_ADDITIONAL_WORKSPACE_BYTES


@dataclass(frozen=True)
class _CommandResult:
    return_code: int
    output: str
    timed_out: bool = False
    resource_limited: bool = False


def _is_secret_file(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env.example":
        return False
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or lowered in _SECRET_FILES
        or lowered.endswith(_SECRET_SUFFIXES)
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(
            getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_POINT
        )
    except OSError:
        return True


def _snapshot_repo(source: Path, target: Path) -> int:
    """Copy project inputs to a private directory without links or secrets."""
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError(f"Sandbox source is not a directory: {source}")
    target.mkdir(parents=True, exist_ok=False)
    total_bytes = 0
    total_files = 0

    for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        dirs[:] = [
            name
            for name in dirs
            if name not in {".git", ".hg", ".svn", ".selfheal_venv"}
            and name.lower() not in _SECRET_DIRS
            and not _is_secret_file(name)
            and not _is_reparse_point(root_path / name)
        ]
        destination_root = target / relative_root
        destination_root.mkdir(parents=True, exist_ok=True)

        for name in files:
            if name in {".git", ".hg", ".svn"}:
                continue
            if _is_secret_file(name) or _is_reparse_point(root_path / name):
                continue
            source_file = root_path / name
            destination_file = destination_root / name
            try:
                size = source_file.stat().st_size
            except OSError:
                continue
            total_files += 1
            total_bytes += size
            if total_files > MAX_SNAPSHOT_FILES:
                raise SandboxUnavailableError(
                    f"Repository snapshot exceeds the {MAX_SNAPSHOT_FILES:,}-file limit"
                )
            if total_bytes > MAX_SNAPSHOT_BYTES:
                raise SandboxUnavailableError(
                    f"Repository snapshot exceeds {MAX_SNAPSHOT_BYTES // (1024 * 1024)} MiB"
                )
            shutil.copy2(source_file, destination_file, follow_symlinks=False)

    return total_bytes


def _command_text(command: str | Sequence[str]) -> str:
    return command if isinstance(command, str) else " ".join(command)


def _safe_child_environment(workspace: Path, command: str) -> dict[str, str]:
    """Build a small environment with no inherited provider or host secrets."""
    if os.name == "nt":
        runtime_names = _runtime_names(command)
        windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
        system_root = Path(os.environ.get("SYSTEMROOT", str(windows)))
        base_python = Path(sys.base_prefix).resolve()
        path_dirs = [system_root / "System32", windows, base_python, Path(sys.executable).parent]
        runtime_dirs = _runtime_dirs_for_command(command)
        node_executable = shutil.which("node")
        if node_executable and runtime_names.intersection({"node", "npm", "npx"}):
            node_root = Path(node_executable).resolve().parent
            runtime_dirs = [path for path in runtime_dirs if path.resolve() != node_root]
        path_dirs.extend(runtime_dirs)
        path = os.pathsep.join(dict.fromkeys(str(item) for item in path_dirs if item.is_dir()))
        home = workspace / ".sandbox-home"
        for child in (
            home,
            home / "AppData" / "Roaming",
            home / "AppData" / "Local",
            workspace / ".sandbox-tmp",
        ):
            child.mkdir(parents=True, exist_ok=True)
        child_env = {
            "SYSTEMROOT": str(system_root),
            "WINDIR": str(windows),
            "COMSPEC": str(system_root / "System32" / "cmd.exe"),
            "PATH": path,
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "TEMP": str(workspace / ".sandbox-tmp"),
            "TMP": str(workspace / ".sandbox-tmp"),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "HOMEDRIVE": str(workspace.drive),
            "HOMEPATH": str(home)[len(workspace.drive) :],
            "HOME": str(home),
            "CI": "true",
            "NO_COLOR": "1",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_NO_INDEX": "1",
            "npm_config_offline": "true",
        }
        if runtime_names.intersection({"node", "npm", "npx"}):
            # Node's default realpath resolution lstat's the drive root while
            # loading a script. AppContainer correctly denies that root. The
            # snapshot excludes symlinks, so preserving module paths avoids
            # that root lookup without changing the repository's resolution
            # graph or granting access outside the sandbox.
            child_env["NODE_OPTIONS"] = "--preserve-symlinks --preserve-symlinks-main"
        return child_env

    home = workspace / ".sandbox-home"
    temp = workspace / ".sandbox-tmp"
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    path_dirs = [
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path(sys.executable).parent,
    ]
    path_dirs.extend(_runtime_dirs_for_command(command))
    return {
        "HOME": str(home),
        "TMPDIR": str(temp),
        "PATH": os.pathsep.join(dict.fromkeys(str(item) for item in path_dirs if item.is_dir())),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "CI": "true",
        "NO_COLOR": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_INDEX": "1",
        "npm_config_offline": "true",
    }


def _runtime_dirs_for_command(command: str) -> list[Path]:
    names = _runtime_names(command)
    dirs: list[Path] = []
    for name in sorted(names):
        resolved = shutil.which(name)
        if resolved:
            dirs.append(Path(resolved).resolve().parent)
    return dirs


def _runtime_files_for_command(command: str) -> list[Path]:
    names = _runtime_names(command)
    files: list[Path] = []
    for name in sorted(names):
        resolved = shutil.which(name)
        if resolved:
            path = Path(resolved).resolve()
            if path.is_file():
                files.append(path)
    if names.intersection({"npm", "npx"}):
        node_executable = shutil.which("node")
        if node_executable:
            path = Path(node_executable).resolve()
            if path.is_file():
                files.append(path)
    return list(dict.fromkeys(files))


def _runtime_names(command: str) -> set[str]:
    return set(re.findall(r"(?<![\w.-])(git|node|npm|npx|go|java)(?![\w.-])", command.lower()))


def _stage_node_runtime(command: str, workspace: Path) -> Path | None:
    """Copy Node into the sandbox workspace when Node/npm is requested.

    On Windows, the user may not have permission to change ACLs on a Node
    installation under Program Files. The sandbox can expose an isolated
    copy through its own writable workspace instead.
    """
    names = _runtime_names(command)
    if not names.intersection({"node", "npm", "npx"}):
        return None
    node_executable = shutil.which("node")
    if not node_executable:
        return None

    node_source = Path(node_executable).resolve()
    runtime_dir = workspace / ".sandbox-runtime"
    runtime_dir.mkdir(exist_ok=True)
    shutil.copy2(node_source, runtime_dir / "node.exe")
    if names.intersection({"npm", "npx"}):
        for name in sorted(names.intersection({"npm", "npx"})):
            executable = shutil.which(name)
            if executable:
                source_cmd = Path(executable).resolve()
                shutil.copy2(source_cmd, runtime_dir / source_cmd.name)
        npm_package = node_source.parent / "node_modules" / "npm"
        if not npm_package.is_dir():
            raise SandboxUnavailableError("npm CLI package is missing from the Node installation")
        shutil.copytree(npm_package, runtime_dir / "node_modules" / "npm")
    return runtime_dir


def _npm_package_dirs_for_command(command: str) -> list[Path]:
    lowered = command.lower()
    if not re.search(r"(?<![\w.-])(npm|npx)(?![\w.-])", lowered):
        return []
    npm_executable = shutil.which("npm")
    if not npm_executable:
        return []
    package_dir = Path(npm_executable).resolve().parent / "node_modules" / "npm"
    return [package_dir] if package_dir.is_dir() else []


async def run_untrusted_command(
    command: str | Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    merge_stderr: bool = True,
    read_only_paths: Sequence[Path] = (),
    read_only_workspace_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Run a repo-controlled command in a temp copy under an OS sandbox.

    Returned fields intentionally match the existing MCP test result shape.
    No command is ever executed natively when sandbox setup fails.
    """
    limits = SandboxLimits(timeout_seconds=timeout, max_processes=MAX_PROCESSES)
    source = cwd.resolve(strict=True)
    allowed_paths: list[Path] = []
    for candidate in read_only_paths:
        resolved = candidate.resolve(strict=True)
        if resolved == source or source in resolved.parents:
            continue
        if resolved in source.parents:
            raise ValueError("Sandbox read-only path cannot contain the repository source")
        if not resolved.is_file() and not resolved.is_dir():
            raise ValueError("Sandbox read-only path must be a file or directory")
        allowed_paths.append(resolved)
    workspace_paths: list[Path] = []
    for relative_value in read_only_workspace_paths:
        relative = Path(relative_value)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("Sandbox workspace read-only paths must be safe relative paths")
        candidate = source / relative
        resolved = candidate.resolve(strict=True)
        if resolved == source or source not in resolved.parents:
            raise ValueError("Sandbox workspace read-only path must stay below the repository")
        if not candidate.is_file() and not candidate.is_dir():
            raise ValueError("Sandbox workspace read-only path must be a file or directory")
        current = source
        for part in relative.parts:
            current = current / part
            if _is_reparse_point(current):
                raise ValueError("Sandbox workspace read-only paths cannot contain links")
        workspace_paths.append(relative)
    temp_root = Path(tempfile.mkdtemp(prefix="selfheal-exec-sandbox-"))
    workspace = temp_root / "workspace"
    try:
        _snapshot_repo(source, workspace)
        snapshot_read_only_paths = [workspace / relative for relative in workspace_paths]
        if any(not path.exists() for path in snapshot_read_only_paths):
            raise SandboxUnavailableError(
                "A requested read-only path was excluded from the repository snapshot"
            )
        child_env = _safe_child_environment(workspace, _command_text(command))
        if isinstance(command, str):
            child_command: str | list[str] = command.replace(str(source), str(workspace))
        else:
            child_command = [argument.replace(str(source), str(workspace)) for argument in command]
        if sys.platform.startswith("linux"):
            if isinstance(child_command, str):
                child_command = child_command.replace(str(workspace), "/workspace")
            else:
                child_command = [
                    argument.replace(str(workspace), "/workspace") for argument in child_command
                ]

        if os.name == "nt":
            result = await asyncio.to_thread(
                _run_windows_appcontainer,
                child_command,
                cwd=workspace,
                env=child_env,
                limits=limits,
                merge_stderr=merge_stderr,
                read_only_paths=allowed_paths,
                read_only_workspace_paths=snapshot_read_only_paths,
            )
        elif sys.platform.startswith("linux"):
            result = await asyncio.to_thread(
                _run_linux_bubblewrap,
                child_command,
                cwd=workspace,
                env=child_env,
                limits=limits,
                merge_stderr=merge_stderr,
                read_only_paths=allowed_paths,
                read_only_workspace_paths=snapshot_read_only_paths,
            )
        else:
            raise SandboxUnavailableError(
                f"No enforced repository-code sandbox is implemented for {sys.platform}"
            )
        if (
            sys.platform.startswith("linux")
            and result.return_code != 0
            and _bubblewrap_permission_denied(result.output)
        ):
            raise SandboxUnavailableError(
                "Bubblewrap namespace creation was denied by the host: "
                + scrub_text(result.output[-1000:])
            )
        return {
            "passed": result.return_code == 0
            and not result.timed_out
            and not result.resource_limited,
            "timed_out": result.timed_out,
            "resource_limited": result.resource_limited,
            "return_code": result.return_code,
            "output": scrub_text(result.output[-20_000:]),
            "sandbox": "windows-appcontainer-job" if os.name == "nt" else "bubblewrap",
        }
    except SandboxUnavailableError as exc:
        return {
            "passed": False,
            "timed_out": False,
            "resource_limited": False,
            "sandbox_unavailable": True,
            "output": str(exc),
        }
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _run_linux_bubblewrap(
    command: str | Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: SandboxLimits,
    merge_stderr: bool,
    read_only_paths: Sequence[Path] = (),
    read_only_workspace_paths: Sequence[Path] = (),
) -> _CommandResult:
    # Resolve Bubblewrap only from root-owned system package locations. A
    # caller-controlled PATH must not be able to substitute the executable
    # that establishes the security boundary.
    bubblewrap = next(
        (
            str(candidate)
            for candidate in (Path("/usr/bin/bwrap"), Path("/bin/bwrap"))
            if candidate.is_file() and os.access(candidate, os.X_OK)
        ),
        None,
    )
    if not bubblewrap:
        raise SandboxUnavailableError("Bubblewrap (bwrap) is required; refusing native execution")
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        raise SandboxUnavailableError(
            "Bubblewrap execution as root is unsupported; configure a non-root healer worker"
        )

    args = [
        bubblewrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--dir",
        "/workspace",
        "--bind",
        str(cwd),
        "/workspace",
        "--chdir",
        "/workspace",
        "--dir",
        "/etc",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for system_path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if Path(system_path).exists():
            args.extend(["--dir", system_path, "--ro-bind", system_path, system_path])
    mounted_system_roots = {Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")}
    for python_prefix in {Path(sys.base_prefix), Path(sys.prefix)}:
        if not python_prefix.exists() or python_prefix == Path("/usr").resolve():
            continue
        for parent in reversed(python_prefix.parents):
            if parent != Path("/") and parent not in mounted_system_roots:
                args.extend(["--dir", str(parent)])
        args.extend(["--ro-bind", str(python_prefix), str(python_prefix)])
    for system_file in (
        "/etc/ld.so.cache",
        "/etc/passwd",
        "/etc/group",
        "/etc/nsswitch.conf",
    ):
        if Path(system_file).is_file():
            args.extend(["--ro-bind", system_file, system_file])
    for directory in (Path("/etc/ssl/certs"), Path("/etc/ca-certificates")):
        if directory.is_dir():
            parent = directory.parent
            if parent != Path("/etc"):
                args.extend(["--dir", str(parent)])
            args.extend(["--ro-bind", str(directory), str(directory)])
    mounted_roots = {
        Path("/workspace"),
        Path("/proc"),
        Path("/dev"),
        Path("/etc"),
        *mounted_system_roots,
    }
    for read_only_path in read_only_paths:
        for parent in reversed(read_only_path.parents):
            if parent in {Path("/"), Path("/tmp")} or any(
                parent == root or root in parent.parents for root in mounted_roots
            ):
                continue
            args.extend(["--dir", str(parent)])
        args.extend(["--ro-bind", str(read_only_path), str(read_only_path)])
    for read_only_path in read_only_workspace_paths:
        relative = read_only_path.relative_to(cwd)
        args.extend(["--ro-bind", str(read_only_path), str(Path("/workspace") / relative)])
    if isinstance(command, str):
        args.extend(["--", "/bin/sh", "-lc", command])
    else:
        args.extend(["--", *command])

    output_limit = limits.max_output_bytes
    preexec_fn = _linux_resource_limits(limits)
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
        start_new_session=True,
        preexec_fn=preexec_fn,
    )
    import selectors

    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    if proc.stderr is not None:
        selector.register(proc.stderr, selectors.EVENT_READ)
    output = bytearray()
    captured_bytes = 0
    deadline = time.monotonic() + limits.timeout_seconds
    timed_out = False
    resource_limited = False
    last_space_check = time.monotonic()
    base_workspace_size = _directory_size(cwd)
    killpg_name = "killpg"
    sigkill_name = "SIGKILL"
    kill_process_group = getattr(os, killpg_name)
    kill_signal = getattr(signal, sigkill_name)
    try:
        while selector.get_map():
            now = time.monotonic()
            if proc.poll() is None and now >= deadline:
                timed_out = True
                kill_process_group(proc.pid, kill_signal)
            if not resource_limited and now - last_space_check >= 1:
                last_space_check = now
                if (
                    _directory_size(cwd)
                    > base_workspace_size + limits.max_additional_workspace_bytes
                ):
                    resource_limited = True
                    kill_process_group(proc.pid, kill_signal)
            for key, _ in selector.select(timeout=0.1):
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = output_limit - captured_bytes
                captured_bytes += len(chunk)
                # When stdout and stderr are requested separately, stdout is
                # the machine-readable result (for example audit JSON). Drain
                # stderr to avoid pipe deadlocks, but keep it out of output.
                if key.fileobj is proc.stdout and remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > max(0, remaining) and not resource_limited:
                    resource_limited = True
                    if proc.poll() is None:
                        kill_process_group(proc.pid, kill_signal)
        proc.wait()
    finally:
        selector.close()
        if proc.poll() is None:
            kill_process_group(proc.pid, kill_signal)
            proc.wait()
    return _CommandResult(
        return_code=124 if timed_out else (125 if resource_limited else proc.returncode or 0),
        output=output.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        resource_limited=resource_limited,
    )


def _linux_resource_limits(limits: SandboxLimits) -> Callable[[], None]:
    def apply() -> None:
        import resource

        cpu_seconds = max(1, int(limits.timeout_seconds))
        setrlimit_name = "setrlimit"
        cpu_limit_name = "RLIMIT_CPU"
        address_space_limit_name = "RLIMIT_AS"
        file_size_limit_name = "RLIMIT_FSIZE"
        open_files_limit_name = "RLIMIT_NOFILE"
        process_limit_name = "RLIMIT_NPROC"
        set_resource_limit = getattr(resource, setrlimit_name)
        set_resource_limit(getattr(resource, cpu_limit_name), (cpu_seconds, cpu_seconds + 1))
        set_resource_limit(
            getattr(resource, address_space_limit_name),
            (limits.memory_bytes, limits.memory_bytes),
        )
        set_resource_limit(
            getattr(resource, file_size_limit_name), (MAX_FILE_BYTES, MAX_FILE_BYTES)
        )
        set_resource_limit(
            getattr(resource, open_files_limit_name), (MAX_OPEN_FILES, MAX_OPEN_FILES)
        )
        process_limit = getattr(resource, process_limit_name, None)
        if process_limit is not None:
            set_resource_limit(process_limit, (limits.max_processes, limits.max_processes))

    return apply


def _run_windows_appcontainer(
    command: str | Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: SandboxLimits,
    merge_stderr: bool,
    read_only_paths: Sequence[Path] = (),
    read_only_workspace_paths: Sequence[Path] = (),
) -> _CommandResult:
    if not sys.platform.startswith("win"):
        raise SandboxUnavailableError("Windows AppContainer is only available on Windows")
    if read_only_workspace_paths:
        raise SandboxUnavailableError(
            "Windows workspace subpath write-deny is not implemented; refusing execution"
        )
    return _windows_process_boundary(
        command,
        cwd=cwd,
        env=env,
        limits=limits,
        merge_stderr=merge_stderr,
        read_only_paths=read_only_paths,
    )


def _windows_process_boundary(
    command: str | Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    limits: SandboxLimits,
    merge_stderr: bool,
    read_only_paths: Sequence[Path] = (),
) -> _CommandResult:
    """Create one AppContainer process with inherited children in a Job Object."""
    from ctypes import wintypes

    win_dll_name = "WinDLL"
    last_error_name = "get_last_error"
    load_windows_library = getattr(ctypes, win_dll_name)
    get_windows_last_error = getattr(ctypes, last_error_name)
    kernel32 = load_windows_library("kernel32", use_last_error=True)
    userenv = load_windows_library("userenv", use_last_error=True)
    advapi32 = load_windows_library("advapi32", use_last_error=True)
    _configure_windows_api(kernel32, userenv, advapi32)

    HANDLE = wintypes.HANDLE
    LPVOID = ctypes.c_void_p
    SIZE_T = ctypes.c_size_t

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", LPVOID),
            ("hStdInput", HANDLE),
            ("hStdOutput", HANDLE),
            ("hStdError", HANDLE),
        ]

    class STARTUPINFOEX(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", LPVOID)]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", LPVOID),
            ("Capabilities", LPVOID),
            ("CapabilityCount", wintypes.DWORD),
            ("Reserved", wintypes.DWORD),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", ctypes.c_void_p),
            ("hThread", ctypes.c_void_p),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (field, ctypes.c_ulonglong)
            for field in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class JOB_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", SIZE_T),
            ("MaximumWorkingSetSize", SIZE_T),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", SIZE_T),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOB_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOB_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]

    def check_win(result: Any, operation: str) -> Any:
        if not result:
            error = get_windows_last_error()
            raise SandboxUnavailableError(
                f"Windows sandbox {operation} failed with Win32 error {error}"
            )
        return result

    def check_handle(result: Any, operation: str) -> Any:
        invalid = ctypes.c_void_p(-1).value
        if not result or result == invalid:
            error = get_windows_last_error()
            raise SandboxUnavailableError(
                f"Windows sandbox {operation} failed with Win32 error {error}"
            )
        return result

    profile_name = f"SelfhealSandbox-{os.getpid()}-{os.urandom(8).hex()}"
    display_name = "Temporary selfheal repository-code sandbox"
    sid = LPVOID()
    hr = userenv.CreateAppContainerProfile(
        profile_name,
        display_name,
        display_name,
        None,
        0,
        ctypes.byref(sid),
    )
    if hr != 0:
        raise SandboxUnavailableError(f"Windows AppContainer creation failed (HRESULT {hr:#x})")

    sid_text_ptr = LPVOID()
    job = HANDLE()
    process_info = PROCESS_INFORMATION()
    attr_list = LPVOID()
    inherit_handles: list[Any] = []
    acl_paths: list[tuple[Path, str, bool]] = []
    stdout_path = cwd / ".sandbox-stdout.log"
    stderr_path = cwd / ".sandbox-stderr.log"
    initial_size = _directory_size(cwd)
    last_space_check = time.monotonic()
    deadline = 0.0
    timed_out = False
    resource_limited = False
    exit_code = 1

    try:
        check_win(
            advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text_ptr)),
            "SID conversion",
        )
        sid_text = ctypes.wstring_at(sid_text_ptr)
        access = "(OI)(CI)"
        # Pytest and some runtimes inspect the sandbox root's parent while
        # resolving their working directory. This parent is a fresh temporary
        # directory containing only this workspace, so grant read/traverse
        # there while keeping the rest of the host temp tree inaccessible.
        _grant_acl(cwd.parent, sid_text, "RX")
        acl_paths.append((cwd.parent, sid_text, False))
        _grant_acl(cwd, sid_text, f"{access}F")
        acl_paths.append((cwd, sid_text, False))
        command_text = _command_text(command)
        staged_node_runtime = _stage_node_runtime(command_text, cwd)
        if staged_node_runtime:
            env["PATH"] = os.pathsep.join([str(staged_node_runtime), env.get("PATH", "")]).rstrip(
                os.pathsep
            )

        base_prefix = Path(sys.base_prefix).resolve()
        active_prefix = Path(sys.prefix).resolve()
        runtime_paths = {
            base_prefix,
            active_prefix,
            Path(sys.executable).resolve().parent,
        }
        node_executable = shutil.which("node") if staged_node_runtime else None
        node_runtime_root = Path(node_executable).resolve().parent if node_executable else None
        for path in _runtime_dirs_for_command(command_text):
            resolved_path = path.resolve()
            if resolved_path != node_runtime_root:
                runtime_paths.add(resolved_path)
        effective_runtime_paths: list[Path] = []
        for runtime_path in sorted(runtime_paths, key=lambda item: len(str(item))):
            if any(
                parent == runtime_path or parent in runtime_path.parents
                for parent in effective_runtime_paths
            ):
                continue
            if runtime_path.exists() and runtime_path != cwd:
                _grant_acl(runtime_path, sid_text, f"{access}RX")
                acl_paths.append((runtime_path, sid_text, False))
                effective_runtime_paths.append(runtime_path)

        # A venv interpreter reads pyvenv.cfg from its prefix before it can
        # locate the base runtime. Grant that existing file directly because
        # an inheritable ACE on the venv directory is not guaranteed to reach
        # files created before the ACL was applied.
        if active_prefix != base_prefix:
            venv_config = active_prefix / "pyvenv.cfg"
            if venv_config.is_file():
                _grant_acl(venv_config, sid_text, "RX")
                acl_paths.append((venv_config, sid_text, False))

        # Existing files do not always inherit an ACE when icacls updates a
        # parent directory. Grant the AppContainer direct read/execute access
        # to the specific runtime launchers it needs. npm's JS CLI is read
        # from its own package tree, so allow that tree only when npm/npx is
        # requested and remove those ACLs recursively during cleanup.
        for runtime_file in _runtime_files_for_command(command_text):
            if staged_node_runtime and runtime_file.parent == node_runtime_root:
                continue
            _grant_acl(runtime_file, sid_text, "RX")
            acl_paths.append((runtime_file, sid_text, False))
        for package_dir in _npm_package_dirs_for_command(command_text):
            if staged_node_runtime and package_dir.parent.parent == node_runtime_root:
                continue
            _grant_acl(package_dir, sid_text, f"{access}RX", recursive=True)
            acl_paths.append((package_dir, sid_text, True))
        for read_only_path in read_only_paths:
            if read_only_path.is_dir():
                _grant_acl(read_only_path, sid_text, f"{access}RX", recursive=True)
                acl_paths.append((read_only_path, sid_text, True))
            else:
                _grant_acl(read_only_path, sid_text, "RX")
                acl_paths.append((read_only_path, sid_text, False))

        attr_size = SIZE_T()
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(attr_size))
        attr_list = kernel32.HeapAlloc(kernel32.GetProcessHeap(), 0, attr_size)
        if not attr_list:
            raise SandboxUnavailableError("Windows sandbox attribute-list allocation failed")
        check_win(
            kernel32.InitializeProcThreadAttributeList(attr_list, 2, 0, ctypes.byref(attr_size)),
            "process attribute initialization",
        )

        log_security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        output_handle = check_handle(
            kernel32.CreateFileW(
                str(stdout_path),
                0x40000000,
                0x00000001 | 0x00000002,
                ctypes.byref(log_security),
                2,
                0x00000080,
                None,
            ),
            "sandbox output file creation",
        )
        inherit_handles.append(output_handle)
        nul_handle = check_handle(
            kernel32.CreateFileW(
                "NUL",
                0x80000000,
                0x00000001 | 0x00000002,
                ctypes.byref(log_security),
                3,
                0x00000080,
                None,
            ),
            "sandbox stdin creation",
        )
        inherit_handles.append(nul_handle)
        error_handle = output_handle
        if not merge_stderr:
            error_handle = check_handle(
                kernel32.CreateFileW(
                    str(stderr_path),
                    0x40000000,
                    0x00000001 | 0x00000002,
                    ctypes.byref(log_security),
                    2,
                    0x00000080,
                    None,
                ),
                "sandbox stderr file creation",
            )
            inherit_handles.append(error_handle)

        capabilities = SECURITY_CAPABILITIES(sid, None, 0, 0)
        handle_array = (HANDLE * len(inherit_handles))(*inherit_handles)
        check_win(
            kernel32.UpdateProcThreadAttribute(
                attr_list,
                0,
                LPVOID(0x00020009),
                ctypes.byref(capabilities),
                ctypes.sizeof(capabilities),
                None,
                None,
            ),
            "AppContainer attribute setup",
        )
        check_win(
            kernel32.UpdateProcThreadAttribute(
                attr_list,
                0,
                LPVOID(0x00020002),
                ctypes.byref(handle_array),
                ctypes.sizeof(handle_array),
                None,
                None,
            ),
            "handle allowlist setup",
        )

        startup = STARTUPINFOEX()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEX)
        startup.StartupInfo.dwFlags = 0x00000100
        startup.StartupInfo.hStdInput = nul_handle
        startup.StartupInfo.hStdOutput = output_handle
        startup.StartupInfo.hStdError = error_handle
        startup.lpAttributeList = attr_list

        job = check_handle(kernel32.CreateJobObjectW(None, None), "Job Object creation")
        job_limits = JOB_EXTENDED_LIMIT_INFORMATION()
        job_limits.BasicLimitInformation.LimitFlags = (
            0x00000004 | 0x00000008 | 0x00000100 | 0x00000200 | 0x00002000
        )
        job_limits.BasicLimitInformation.PerJobUserTimeLimit = int(
            max(1, limits.timeout_seconds) * 10_000_000
        )
        job_limits.BasicLimitInformation.ActiveProcessLimit = limits.max_processes
        job_limits.ProcessMemoryLimit = limits.memory_bytes
        job_limits.JobMemoryLimit = limits.memory_bytes
        check_win(
            kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(job_limits), ctypes.sizeof(job_limits)
            ),
            "resource limit configuration",
        )

        if isinstance(command, str):
            # cmd.exe has its own /S /C quoting rules; CRT escaping via
            # list2cmdline turns embedded quotes into backslash-quote pairs,
            # which cmd.exe does not parse as quoting. The extra outer quote
            # pair is the documented /S /C form for commands that themselves
            # contain quoted executable paths and arguments.
            executable = env["COMSPEC"]
            command_line_text = f'"{executable}" /D /S /C "{command}"'
        else:
            args = list(command)
            executable = args[0]
            command_line_text = subprocess.list2cmdline(args)
        command_line = ctypes.create_unicode_buffer(command_line_text)
        environment_block = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0\0"
        )
        check_win(
            kernel32.CreateProcessW(
                executable,
                command_line,
                None,
                None,
                True,
                0x00000004 | 0x00080000 | 0x00000400 | 0x08000000,
                ctypes.cast(environment_block, LPVOID),
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ),
            "AppContainer process creation",
        )
        check_win(kernel32.AssignProcessToJobObject(job, process_info.hProcess), "Job assignment")
        assigned_to_job = True
        check_win(kernel32.ResumeThread(process_info.hThread) != 0xFFFFFFFF, "process resume")
        deadline = time.monotonic() + limits.timeout_seconds

        while True:
            wait_result = kernel32.WaitForSingleObject(process_info.hProcess, 100)
            if wait_result == 0:
                break
            if wait_result != 0x00000102:
                raise SandboxUnavailableError(
                    "Windows sandbox process wait failed with Win32 error "
                    f"{get_windows_last_error()}"
                )
            if time.monotonic() >= deadline:
                timed_out = True
                kernel32.TerminateJobObject(job, 124)
                kernel32.WaitForSingleObject(process_info.hProcess, 5000)
                break
            try:
                output_size = stdout_path.stat().st_size
                if not merge_stderr and stderr_path.exists():
                    output_size += stderr_path.stat().st_size
            except OSError:
                output_size = 0
            if output_size > limits.max_output_bytes:
                resource_limited = True
                kernel32.TerminateJobObject(job, 125)
                kernel32.WaitForSingleObject(process_info.hProcess, 5000)
                break
            now = time.monotonic()
            if now - last_space_check >= 1.0:
                last_space_check = now
                if _directory_size(cwd) > initial_size + limits.max_additional_workspace_bytes:
                    resource_limited = True
                    kernel32.TerminateJobObject(job, 125)
                    kernel32.WaitForSingleObject(process_info.hProcess, 5000)
                    break

        code = wintypes.DWORD()
        check_win(
            kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(code)),
            "exit-code read",
        )
        exit_code = int(code.value)
        try:
            output = stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        return _CommandResult(
            return_code=exit_code,
            output=output[: limits.max_output_bytes],
            timed_out=timed_out,
            resource_limited=resource_limited,
        )
    finally:
        if process_info.hProcess and not locals().get("assigned_to_job", False):
            kernel32.TerminateProcess(process_info.hProcess, 125)
            kernel32.WaitForSingleObject(process_info.hProcess, 5000)
        if job:
            kernel32.CloseHandle(job)  # KILL_ON_JOB_CLOSE contains descendants
        if process_info.hThread:
            kernel32.CloseHandle(process_info.hThread)
        if process_info.hProcess:
            kernel32.CloseHandle(process_info.hProcess)
        for handle in inherit_handles:
            kernel32.CloseHandle(handle)
        if attr_list:
            kernel32.DeleteProcThreadAttributeList(attr_list)
            kernel32.HeapFree(kernel32.GetProcessHeap(), 0, attr_list)
        for path, package_sid, recursive in reversed(acl_paths):
            _remove_acl(path, package_sid, recursive=recursive)
        if sid_text_ptr:
            kernel32.LocalFree(sid_text_ptr)
        userenv.DeleteAppContainerProfile(profile_name)
        if sid:
            advapi32.FreeSid(sid)


def _icacls_executable() -> str | None:
    icacls = shutil.which("icacls.exe") or shutil.which("icacls")
    if icacls:
        return icacls
    if os.name == "nt":
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        candidate = system_root / "System32" / "icacls.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _grant_acl(path: Path, sid: str, permissions: str, *, recursive: bool = False) -> None:
    icacls = _icacls_executable()
    if not icacls:
        raise SandboxUnavailableError("icacls is required for Windows AppContainer ACL setup")
    args = [icacls, str(path), "/grant", f"*{sid}:{permissions}", "/L"]
    if recursive:
        args.append("/T")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    failed_processing = re.search(
        r"Failed processing\s+([1-9]\d*)", result.stdout + result.stderr, re.IGNORECASE
    )
    if result.returncode or failed_processing:
        detail = scrub_text((result.stdout + result.stderr).strip())[-400:]
        raise SandboxUnavailableError(
            "Windows AppContainer ACL grant failed for an allowlisted path "
            f"(exit code {result.returncode}): {detail}"
        )


def _remove_acl(path: Path, sid: str, *, recursive: bool = False) -> None:
    icacls = _icacls_executable()
    if not icacls:
        return
    args = [icacls, str(path), "/remove:g", f"*{sid}", "/C", "/Q", "/L"]
    if recursive:
        args.append("/T")
    subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )


def _configure_windows_api(kernel32: Any, userenv: Any, advapi32: Any) -> None:
    """Declare pointer-sized Win32 signatures before any native call."""
    from ctypes import wintypes

    handle = wintypes.HANDLE
    lpvoid = ctypes.c_void_p
    size_t = ctypes.c_size_t
    bool_type = wintypes.BOOL
    dword = wintypes.DWORD

    userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        lpvoid,
        dword,
        ctypes.POINTER(lpvoid),
    ]
    userenv.CreateAppContainerProfile.restype = wintypes.LONG
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = wintypes.LONG
    advapi32.ConvertSidToStringSidW.argtypes = [lpvoid, ctypes.POINTER(lpvoid)]
    advapi32.ConvertSidToStringSidW.restype = bool_type

    kernel32.InitializeProcThreadAttributeList.argtypes = [
        lpvoid,
        dword,
        dword,
        ctypes.POINTER(size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = bool_type
    kernel32.UpdateProcThreadAttribute.argtypes = [
        lpvoid,
        dword,
        lpvoid,
        lpvoid,
        size_t,
        lpvoid,
        lpvoid,
    ]
    kernel32.UpdateProcThreadAttribute.restype = bool_type
    kernel32.DeleteProcThreadAttributeList.argtypes = [lpvoid]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.GetProcessHeap.argtypes = []
    kernel32.GetProcessHeap.restype = handle
    kernel32.HeapAlloc.argtypes = [handle, dword, size_t]
    kernel32.HeapAlloc.restype = lpvoid
    kernel32.HeapFree.argtypes = [handle, dword, lpvoid]
    kernel32.HeapFree.restype = bool_type
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        dword,
        dword,
        lpvoid,
        dword,
        dword,
        handle,
    ]
    kernel32.CreateFileW.restype = handle
    kernel32.CreateJobObjectW.argtypes = [lpvoid, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = handle
    kernel32.SetInformationJobObject.argtypes = [handle, dword, lpvoid, dword]
    kernel32.SetInformationJobObject.restype = bool_type
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        lpvoid,
        lpvoid,
        bool_type,
        dword,
        lpvoid,
        wintypes.LPCWSTR,
        lpvoid,
        lpvoid,
    ]
    kernel32.CreateProcessW.restype = bool_type
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel32.AssignProcessToJobObject.restype = bool_type
    kernel32.ResumeThread.argtypes = [handle]
    kernel32.ResumeThread.restype = dword
    kernel32.WaitForSingleObject.argtypes = [handle, dword]
    kernel32.WaitForSingleObject.restype = dword
    kernel32.TerminateJobObject.argtypes = [handle, wintypes.UINT]
    kernel32.TerminateJobObject.restype = bool_type
    kernel32.TerminateProcess.argtypes = [handle, wintypes.UINT]
    kernel32.TerminateProcess.restype = bool_type
    kernel32.GetExitCodeProcess.argtypes = [handle, ctypes.POINTER(dword)]
    kernel32.GetExitCodeProcess.restype = bool_type
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = bool_type
    kernel32.LocalFree.argtypes = [handle]
    kernel32.LocalFree.restype = handle
    advapi32.FreeSid.argtypes = [lpvoid]
    advapi32.FreeSid.restype = lpvoid


def _directory_size(root: Path) -> int:
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                    if getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += info.st_size
                except OSError:
                    continue
    return total
