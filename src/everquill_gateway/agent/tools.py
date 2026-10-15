import os
import subprocess
from pathlib import Path, PureWindowsPath

MAX_CONTEXT_FILE_BYTES = 256_000
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".toml", ".json", ".yaml", ".yml",
    ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".html",
    ".sh", ".ini", ".cfg", ".rst", ".go", ".rs", ".java",
    ".c", ".cpp", ".h",
}

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".hermes", ".codex", ".claude",
    ".agents", ".config", "__pycache__", "node_modules", ".venv",
    "venv", "dist", "build", "logs", "sessions",
}


def _safe_root(root=None):
    return Path(root or os.getcwd()).resolve()


def _git_ignored(root, paths):
    """Ask Git about both tracked and untracked ignore matches in one call."""
    if not paths:
        return set()
    payload = b"\0".join(os.fsencode(str(path)) for path in paths) + b"\0"
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"],
            cwd=root, input=payload, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("could not check project ignore rules") from exc
    if result.returncode not in (0, 1):
        has_ignore_rules = any(
            (root / parent / ".gitignore").is_file()
            for path in paths for parent in Path(path).parents
        )
        if has_ignore_rules:
            raise ValueError("cannot apply project .gitignore rules")
        return set()
    return {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}


def _safe_path(root, path, ignored=None):
    windows = PureWindowsPath(path)
    if windows.drive or windows.root or ".." in windows.parts:
        raise ValueError("path escapes project root")
    root = root.resolve()
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path escapes project root")
    candidate = root / relative
    if any(part.is_symlink() for part in (candidate, *candidate.parents) if part != root):
        raise ValueError("symlink paths are not allowed")
    target = candidate.resolve()
    try:
        parts = target.relative_to(root).parts
    except ValueError as exc:
        raise ValueError("path escapes project root") from exc
    if any(part.lower() in IGNORED_DIRS or part.startswith(".") for part in parts[:-1]):
        raise ValueError("private project path")
    name = parts[-1].lower() if parts else ""
    if (name.startswith(".") or ".env" in name or ".bak" in name
            or name.startswith(("cookies.", "credentials.", "secrets.",
                                "config.local.", "id_rsa", "id_ed25519"))
            or name.endswith((".log", ".orig", ".swp", ".key", ".pem",
                              ".p12", ".pfx", ".har", ".sqlite", ".sqlite3", ".db"))
            or name in {"config.json", "cookies.txt", "credentials.json", "secrets.json",
                        "sessions.json", "muse-code-sessions.json"}):
        raise ValueError("private project file")
    if target.is_file():
        if target.suffix.lower() not in TEXT_SUFFIXES and name not in {"readme", "license", "makefile"}:
            raise ValueError("non-text project file")
        if target.stat().st_size > MAX_CONTEXT_FILE_BYTES:
            raise ValueError("project file exceeds context size limit")
        data = target.read_bytes()
        if b"\0" in data:
            raise ValueError("binary project file")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("non-UTF-8 project file") from exc
    matches = _git_ignored(root, [relative]) if ignored is None else ignored
    if str(relative) in matches:
        raise ValueError("ignored project file")
    return target


def _admitted(root, path, ignored):
    try:
        _safe_path(root, path, ignored=ignored)
        return True
    except (ValueError, OSError):
        return False


def glob_files(pattern="**/*", root=None, limit=200):
    root = _safe_root(root)
    candidates = [path for path in root.glob(pattern) if path.is_file()]
    ignored = _git_ignored(root, [path.relative_to(root) for path in candidates])
    results = []

    for path in candidates:
        if not _admitted(root, path.relative_to(root), ignored):
            continue

        results.append(str(path.relative_to(root)))

        if len(results) >= limit:
            break

    return results


def read_file(path, root=None, max_chars=30000):
    root = _safe_root(root)
    target = _safe_path(root, path)

    if not target.exists():
        raise FileNotFoundError(path)

    if not target.is_file():
        raise ValueError(f"not a file: {path}")

    text = target.read_text(encoding="utf-8", errors="replace")

    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]..."

    return text


def grep_files(pattern, root=None, limit=100):
    root = _safe_root(root)
    candidates = [path for path in root.rglob("*") if path.is_file()]
    ignored = _git_ignored(root, [path.relative_to(root) for path in candidates])
    results = []

    for path in candidates:
        if not _admitted(root, path.relative_to(root), ignored):
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.lower() in line.lower():
                results.append({
                    "file": str(path.relative_to(root)),
                    "line": lineno,
                    "text": line[:500],
                })

                if len(results) >= limit:
                    return results

    return results
