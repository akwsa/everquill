"""Run an existing Python project's tests after an approved edit."""

import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib

from pathlib import Path
from pathlib import PureWindowsPath
from typing import NamedTuple


class CommandDecision(NamedTuple):
    allowed: bool
    status: str
    reason: str
    argv: tuple[str, ...]


class DiscoveryResult(NamedTuple):
    """Deterministic, non-executing project test discovery result."""

    status: str
    language: str
    framework: str
    command: tuple[str, ...]
    paths: tuple[str, ...]
    reason: str


def _has_shell_metacharacter(value):
    return any(character in value for character in ";&|><$()`\n\r")


def _is_link(info):
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, "st_file_attributes", 0) &
                 getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _unlinked_path(path):
    path = Path(path)
    windows = PureWindowsPath(path)
    if (".." in path.parts or ".." in windows.parts or
            (windows.drive and (os.name != "nt" or not windows.is_absolute()))):
        raise ValueError("unsafe execution path")
    path = path.absolute()
    for component in (*reversed(path.parents), path):
        if _is_link(component.lstat()):
            raise ValueError("linked execution path")
    return path.resolve(strict=True)


def _execution_root(root):
    root = _unlinked_path(root)
    if not root.is_dir():
        raise ValueError("execution root is not a directory")
    return root


def _workspace_argument(root, value):
    relative = PureWindowsPath(value)
    if relative.root or relative.drive or ".." in relative.parts:
        return None
    try:
        candidate = _unlinked_path(root / Path(*relative.parts))
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate


def validate_command(command, root):
    """Validate argv shape and shell/path syntax before classification."""
    if not isinstance(command, (list, tuple)) or not command:
        return CommandDecision(False, "REJECTED", "command must be an argv list", ())
    if any(not isinstance(value, str) or _has_shell_metacharacter(value)
           for value in command):
        return CommandDecision(False, "REJECTED", "shell syntax is not allowed", tuple(command))
    return CommandDecision(True, "VALID", "argv is structurally valid", tuple(command))


def classify_command(command, root):
    """Allow only fixed, repository-local test and syntax-check command shapes."""
    try:
        root = _execution_root(root)
    except (OSError, ValueError):
        return CommandDecision(False, "REJECTED", "unsafe execution directory", ())
    validation = validate_command(command, root)
    if not validation.allowed:
        return validation
    argv = tuple(command)
    if not Path(argv[0]).is_absolute():
        return CommandDecision(False, "REJECTED", "executable must be an absolute runtime path", ())
    executable = Path(argv[0]).resolve()
    if executable == Path(sys.executable).resolve():
        if len(argv) == 5 and argv[1:4] == ("-m", "pytest", "-q") and argv[4] in (".", "tests"):
            if _workspace_argument(root, argv[4]) and (root / argv[4]).is_dir():
                return CommandDecision(True, "ALLOWED", "known Python test command", argv)
        if (len(argv) == 9 and
                argv[1:5] == ("-m", "unittest", "discover", "-s") and
                argv[5] in (".", "tests") and
                argv[6:9] == ("-p", "test_*.py", "-v")):
            if _workspace_argument(root, argv[5]) and (root / argv[5]).is_dir():
                return CommandDecision(True, "ALLOWED", "known Python test command", argv)
        if (len(argv) == 4 and argv[1:3] == ("-m", "py_compile") and
                argv[3].lower().endswith(".py")):
            target = _workspace_argument(root, argv[3])
            if target and target.is_file():
                return CommandDecision(True, "ALLOWED", "workspace Python syntax check", argv)
        return CommandDecision(False, "REJECTED", "command is not an allowed project test", argv)

    node = shutil.which("node")
    if node and executable == Path(node).resolve():
        if len(argv) == 3 and argv[1] == "--check" and argv[2] == "src/everquill/tui.mjs":
            if _workspace_argument(root, argv[2]) and (root / argv[2]).is_file():
                return CommandDecision(True, "ALLOWED", "known Node syntax check", argv)
        if len(argv) == 3 and argv[1:3] == ("--test", "tests/test_tui.mjs"):
            if _workspace_argument(root, argv[2]) and (root / argv[2]).is_file():
                return CommandDecision(True, "ALLOWED", "known Node test command", argv)
        if (len(argv) >= 3 and argv[1] == "--test" and
                all(_workspace_argument(root, value) and
                    (root / PureWindowsPath(value)).is_file()
                    for value in argv[2:]) and
                all(Path(value).suffix.lower() in {".js", ".mjs", ".cjs"}
                    for value in argv[2:])):
            return CommandDecision(True, "ALLOWED", "known Node test command", argv)
        return CommandDecision(False, "REJECTED", "command is not an allowed Node check or test", argv)

    if executable.name.lower() in {"cmd.exe", "powershell.exe", "pwsh.exe", "bash", "sh"}:
        return CommandDecision(False, "REJECTED", "shell executables are not allowed", argv)
    return CommandDecision(False, "REJECTED", "command is not an allowed project test", argv)


class TestResult:
    """Stable result shape used by the write and rollback pipeline."""

    def __init__(self, passed, command, output, status=None, returncode=None,
                 side_effects=None, isolation="NOT_RUN"):
        self.passed = bool(passed)
        self.command = command
        self.output = output
        self.status = status or ("PASS" if self.passed else "FAIL")
        self.returncode = returncode
        self.side_effects = side_effects or {"unexpected": (), "proposal_targets": ()}
        self.isolation = isolation
        self.execution_state = None
        self.state_cleanup = "NOT_CREATED"
        self.discovery = None


_OUTPUT_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"auth[_-]?token|secret(?:[_-]?key)?|password|passwd|private[_-]?key|"
        r"authorization)\b\s*(?:=|:)\s*)([^\s,;]+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/-]{12,}"),
    re.compile(r"\b(?:sk-|ghp_|github_pat_|glpat-|xox[baprs]-)[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?-----END [A-Z0-9 ]+ PRIVATE KEY-----", re.S),
)


def _sanitize_output(value):
    value = str(value or "")
    for pattern in _OUTPUT_SECRET_PATTERNS:
        if pattern.groups:
            value = pattern.sub(lambda match: match.group(1) + "[REDACTED]", value)
        else:
            value = pattern.sub("[REDACTED]", value)
    return value.strip()[-6000:]


def _system_environment():
    if os.name != "nt":
        return {}
    system_root = os.environ.get("SystemRoot")
    if not system_root or not Path(system_root).is_absolute():
        raise OSError("Windows runtime location unavailable")
    return {"SYSTEMROOT": system_root, "WINDIR": system_root}


def _controlled_environment(state):
    """Build runtime defaults without copying the caller's home or tool settings."""
    state = Path(state)
    paths = {}
    for name in ("home", "config", "local", "cache", "tmp", "pycache"):
        paths[name] = state / name
        paths[name].mkdir(mode=0o700)
    environment = _system_environment()
    runtime_paths = [str(Path(sys.executable).parent)]
    node = shutil.which("node")
    if node:
        runtime_paths.append(str(Path(node).parent))
    if os.name == "nt":
        system = Path(environment["SYSTEMROOT"])
        runtime_paths.extend((str(system / "System32"), str(system)))
    else:
        runtime_paths.extend(path for path in os.defpath.split(os.pathsep)
                             if Path(path).is_absolute())
    environment.update({
        "PATH": os.pathsep.join(dict.fromkeys(runtime_paths)),
        "HOME": str(paths["home"]), "USERPROFILE": str(paths["home"]),
        "APPDATA": str(paths["config"]), "LOCALAPPDATA": str(paths["local"]),
        "XDG_CONFIG_HOME": str(paths["config"]), "XDG_DATA_HOME": str(paths["local"]),
        "XDG_CACHE_HOME": str(paths["cache"]),
        "TEMP": str(paths["tmp"]), "TMP": str(paths["tmp"]), "TMPDIR": str(paths["tmp"]),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(paths["pycache"]), "PYTHONIOENCODING": "utf-8",
    })
    if os.name == "nt":
        drive, home_path = os.path.splitdrive(str(paths["home"]))
        environment.update(HOMEDRIVE=drive, HOMEPATH=home_path, PATHEXT=".COM;.EXE")
    return environment


def _directory_identity(path):
    path = _execution_root(path)
    info = path.stat()
    return info.st_dev, info.st_ino


def _cleanup_execution_state(state, owned):
    """Remove only unchanged, empty directories we created; never recurse."""
    if state not in owned:
        return "RETAINED"
    for path, identity in reversed(tuple(owned.items())):
        try:
            if (_directory_identity(state) != owned[state] or
                    _directory_identity(path) != identity):
                return "RETAINED"
            path.rmdir()
        except (OSError, ValueError):
            return "RETAINED"
    return "REMOVED"


def _isolation_unavailable():
    return TestResult(False, "", "Requested execution isolation is unavailable; "
                      "no command was started and no weaker mode was used.",
                      "ISOLATION_UNAVAILABLE", isolation="UNAVAILABLE")


def _capture_output(stdout, stderr):
    """Combine process streams and sanitize before exposing them to the CLI."""
    return _sanitize_output("".join(
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        for value in (stdout, stderr)
    ))


def _evaluate_exit_code(returncode, output, display):
    """Map an approved process exit into the runner's explicit status."""
    if re.search(r"(?:Ran 0 tests|no tests ran|collected 0 items)", output, re.I):
        return TestResult(False, display, output, "FAIL", returncode)
    status = "PASS" if returncode == 0 else "FAIL"
    return TestResult(returncode == 0, display, output, status, returncode)


def _filesystem_state(root):
    """Record workspace entry identity and file hashes without reading content out."""
    state = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in tuple(dirnames):
            path = base / name
            if _is_link(path.lstat()):
                info = path.lstat()
                state[path.relative_to(root).as_posix()] = (
                    "link", info.st_dev, info.st_ino, info.st_mtime_ns,
                )
                dirnames.remove(name)
        for name in dirnames:
            path = base / name
            relative = path.relative_to(root).as_posix()
            info = path.stat()
            state[relative] = ("directory", info.st_dev, info.st_ino)
        for name in filenames:
            path = base / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            identity = (info.st_dev, info.st_ino, info.st_size,
                        info.st_mtime_ns, info.st_mode)
            if _is_link(info):
                state[relative] = ("symlink", identity)
                continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            after = path.lstat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                    after.st_mode):
                raise OSError("workspace file changed during snapshot")
            state[relative] = ("file", identity, digest.hexdigest())
    return state


def _filesystem_changes(before, after, expected_targets):
    changed = tuple(sorted(path for path in before.keys() | after.keys()
                           if before.get(path) != after.get(path)))
    targets = set(expected_targets)
    proposal_targets = tuple(path for path in changed if path in targets)
    unexpected = tuple(path for path in changed if path not in targets)
    return {"unexpected": unexpected, "proposal_targets": proposal_targets}


def _stop_process_tree(process, *, diagnostics=None):
    """Attempt tree termination; do not claim Windows descendant confirmation."""
    if os.name == "nt":
        if diagnostics is not None:
            diagnostics.update(taskkill_returncode=None, taskkill_stdout="",
                               taskkill_stderr="", taskkill_error=None,
                               fallback_executed=False, wait_result=None,
                               poll_result=None)
        try:
            environment = _system_environment()
            taskkill = Path(environment["SYSTEMROOT"]) / "System32" / "taskkill.exe"
            try:
                result = subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=5, shell=False, check=False,
                    env=environment, stdin=subprocess.DEVNULL, close_fds=True,
                )
            except subprocess.TimeoutExpired as exc:
                if diagnostics is not None:
                    diagnostics.update(taskkill_error="timeout",
                                       taskkill_stdout=_capture_output(exc.stdout, None),
                                       taskkill_stderr=_capture_output(None, exc.stderr))
                raise
            except OSError:
                if diagnostics is not None:
                    diagnostics["taskkill_error"] = "os_error"
                raise
            if diagnostics is not None:
                diagnostics.update(taskkill_returncode=result.returncode,
                                   taskkill_stdout=_capture_output(result.stdout, None),
                                   taskkill_stderr=_capture_output(None, result.stderr))
            if result.returncode != 0:
                if diagnostics is not None:
                    diagnostics["fallback_executed"] = True
                process.kill()
                waited = process.wait(timeout=5)
                if diagnostics is not None:
                    diagnostics.update(wait_result=waited, poll_result=process.poll())
                return False
            waited = process.wait(timeout=5)
            polled = process.poll()
            if diagnostics is not None:
                diagnostics.update(wait_result=waited, poll_result=polled)
            # taskkill success and root exit do not account for descendants.
            return False
        except (OSError, subprocess.TimeoutExpired):
            try:
                if diagnostics is not None:
                    diagnostics["fallback_executed"] = True
                process.kill()
                waited = process.wait(timeout=5)
                if diagnostics is not None:
                    diagnostics.update(wait_result=waited, poll_result=process.poll())
            except (OSError, subprocess.TimeoutExpired):
                pass
            return False
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
    except OSError:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return process.poll() is not None


def _run_approved_command(argv, root, environment, timeout, display):
    """Spawn one approved command and report timeout/interruption cleanup state."""
    try:
        process_options = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process = subprocess.Popen(
            list(argv),
            cwd=root,
            env=environment,
            shell=False,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **process_options,
        )
    except OSError:
        return TestResult(False, display, "Runner could not start the approved command.", "RUNNER_ERROR")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return TestResult(
            process.returncode == 0, display,
            _capture_output(stdout, stderr),
            "PASS" if process.returncode == 0 else "FAIL", process.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        cleaned = _stop_process_tree(process)
        partial = _capture_output(exc.stdout, exc.stderr)
        status = "TIMEOUT" if cleaned else "TERMINATION_FAILED"
        return TestResult(False, display, partial, status, process.poll())
    except KeyboardInterrupt:
        cleaned = _stop_process_tree(process)
        status = "CANCELLED" if cleaned else "TERMINATION_FAILED"
        return TestResult(False, display, "Command cancelled; process cleanup was " +
                          ("confirmed." if cleaned else "incomplete."), status,
                          process.poll())
    except (OSError, ValueError):
        cleaned = _stop_process_tree(process)
        return TestResult(False, display, "Command output capture failed; process cleanup was " +
                          ("confirmed." if cleaned else "incomplete."),
                          "RUNNER_ERROR" if cleaned else "TERMINATION_FAILED",
                          process.poll(), isolation="LIMITED")


def execute_command(command, root, timeout=120, expected_targets=None, *, isolation="limited"):
    """Validate, classify, execute, capture, and evaluate one safe command."""
    if isolation != "limited":
        return _isolation_unavailable()
    decision = classify_command(command, root)
    if not decision.allowed:
        return TestResult(False, "", decision.reason, "REJECTED")
    display = " ".join(decision.argv)
    expected = {Path(str(target)).as_posix() for target in (expected_targets or ())}
    try:
        root = _execution_root(root)
        root_identity = _directory_identity(root)
        before = _filesystem_state(root)
    except (OSError, ValueError):
        return TestResult(False, display, "Workspace state could not be inspected safely.",
                          "SIDE_EFFECT_SCAN_FAILED")
    state = None
    owned = {}
    result = _isolation_unavailable()
    try:
        state = Path(tempfile.mkdtemp(prefix="everquill-execution-"))
        owned[state] = _directory_identity(state)
        environment = _controlled_environment(state)
        for child in state.iterdir():
            owned[child] = _directory_identity(child)
        if decision.argv[1:3] == ("-m", "pytest"):
            environment["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
        if (_directory_identity(state) != owned[state] or
                _directory_identity(root) != root_identity or
                not classify_command(decision.argv, root).allowed):
            raise ValueError("execution paths changed during preparation")
        result = _run_approved_command(
            decision.argv, root, environment, timeout, display
        )
        if result.status != "RUNNER_ERROR":
            result.isolation = "LIMITED"
    except (OSError, ValueError):
        result = _isolation_unavailable()
    finally:
        if state is not None:
            result.execution_state = str(state)
            result.state_cleanup = ("DEFERRED" if result.status == "TERMINATION_FAILED" else
                                    _cleanup_execution_state(state, owned))
            if result.state_cleanup != "REMOVED":
                result.output += "\nExecution state retained for manual review; no recursive cleanup."
    if result.isolation != "LIMITED":
        return result
    try:
        after = _filesystem_state(root)
    except OSError:
        result.passed = False
        result.output += "\nWorkspace changes may have occurred; post-run inspection failed."
        if result.status != "TERMINATION_FAILED":
            result.status = "SIDE_EFFECT_SCAN_FAILED"
        return result
    side_effects = _filesystem_changes(before, after, expected)
    if side_effects["unexpected"] or side_effects["proposal_targets"]:
        count = len(side_effects["unexpected"]) + len(side_effects["proposal_targets"])
        output = (result.output + "\n" if result.output else "") + \
            f"Detected {count} workspace filesystem change(s); no cleanup was attempted."
        status = result.status
        if status == "PASS":
            status = "SIDE_EFFECTS"
        elif status == "FAIL":
            status = "FAIL_WITH_SIDE_EFFECTS"
        result.passed = False
        result.output = output
        result.status = status
    result.side_effects = side_effects
    return result


def _safe_project_files(root, suffixes):
    """Return sorted conventional test files without following links."""
    found = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames[:] = [name for name in dirnames
                       if name not in {".git", ".venv", "node_modules",
                                       "__pycache__", "dist", "build"}]
        safe_dirs = []
        for name in dirnames:
            if _is_link((base / name).lstat()):
                if name in {"test", "tests"}:
                    raise ValueError("linked test directory")
                continue
            safe_dirs.append(name)
        dirnames[:] = sorted(safe_dirs)
        for name in sorted(filenames):
            path = base / name
            info = path.lstat()
            if _is_link(info):
                if path.suffix.lower() in suffixes:
                    raise ValueError("linked test path")
                continue
            if path.suffix.lower() in suffixes:
                found.append(path)
    return tuple(found)


def _relative_safe_paths(root, paths):
    values = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if _workspace_argument(root, relative) is None:
            raise ValueError("unsafe test path")
        values.append(relative)
    return tuple(values)


def _validate_metadata(root, names):
    """Parse metadata only to reject malformed files; never read commands."""
    for name in names:
        path = root / name
        if not path.exists():
            continue
        try:
            if _is_link(path.lstat()) or not path.is_file():
                raise ValueError(f"linked or invalid {name}")
        except OSError as exc:
            raise ValueError(f"uninspectable {name}") from exc
        try:
            if name == "package.json":
                json.loads(path.read_text(encoding="utf-8"))
            elif name == "pyproject.toml":
                tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
            raise ValueError(f"malformed {name}")


def discover_test_command(root):
    """Inspect local metadata/layout and return one fixed, authorized shape."""
    try:
        root = _execution_root(root)
        _validate_metadata(root, ("pyproject.toml", "package.json"))
        python_files = _safe_project_files(root, {".py"})
        python_tests = tuple(path for path in python_files
                             if path.name.startswith("test_"))
        node_files = _safe_project_files(root, {".js", ".mjs", ".cjs"})
        node_tests = tuple(path for path in node_files
                           if (path.parent.name in {"test", "tests"} or
                               path.name.startswith("test_") or
                               ".test." in path.name or
                               ".spec." in path.name))
        has_python_marker = any((root / name).is_file() and
                                not _is_link((root / name).lstat())
                                for name in ("pyproject.toml", "pytest.ini",
                                             "setup.cfg", "tox.ini"))
        package = root / "package.json"
        has_node_marker = (package.is_file() and
                           not _is_link(package.lstat()))
        # Python has precedence when both conventional layouts exist. This
        # preserves the repository's existing Python post-write test flow.
        if python_tests:
            paths = _relative_safe_paths(root, python_tests)
            source = "tests" if (root / "tests").is_dir() else "."
            if _workspace_argument(root, source) is None:
                raise ValueError("unsafe test directory")
            return DiscoveryResult("DISCOVERED", "Python", "unittest",
                                   (sys.executable, "-m", "unittest", "discover",
                                    "-s", source, "-p", "test_*.py", "-v"),
                                   paths, "conventional Python test files")
        if node_tests:
            paths = _relative_safe_paths(root, node_tests)
            executable = shutil.which("node")
            if not executable or not Path(executable).is_absolute():
                return DiscoveryResult("UNAVAILABLE", "Node.js", "node:test", (), paths,
                                       "Node.js runtime is unavailable")
            return DiscoveryResult("DISCOVERED", "Node.js", "node:test",
                                   tuple([executable, "--test", *paths]),
                                   paths, "conventional Node test files")
        if has_python_marker or has_node_marker:
            return DiscoveryResult("UNAVAILABLE", "", "", (), (),
                                   "recognized project metadata has no supported test files")
        return DiscoveryResult("UNAVAILABLE", "", "", (), (),
                               "no supported language-specific test layout found")
    except (OSError, ValueError):
        return DiscoveryResult("REJECTED", "", "", (), (),
                               "project metadata or test paths are unsafe")


def run_project_tests(root, timeout=120, expected_targets=None, *, isolation="limited"):
    """Discover one fixed suite, then send it through command authorization."""
    if isolation != "limited":
        return _isolation_unavailable()
    discovery = discover_test_command(root)
    if discovery.status != "DISCOVERED":
        status = discovery.status if discovery.status in {"REJECTED", "AMBIGUOUS"} else "NOT_RUN"
        result = TestResult(False, "", discovery.reason, status)
        result.discovery = discovery
        return result
    result = execute_command(list(discovery.command), root, timeout=timeout,
                             expected_targets=expected_targets, isolation=isolation)
    result.discovery = discovery
    return result
