import argparse
import builtins
import difflib
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import time  # Existing tests patch this shared clock for Muse history polling.
from pathlib import Path, PureWindowsPath

from everquill_gateway.errors import AuthError, GatewayError
from everquill_gateway.agent.context import build_project_context
from everquill_gateway.agent.tools import grep_files, read_file, _safe_path, TEXT_SUFFIXES
from everquill.session import load_session, save_session
from everquill.executor import run_project_tests
from everquill.privacy import contains_sensitive_content
from everquill.reply import require_final_reply


def load_config():
    from everquill_gateway.cli import load_config as gateway_load_config
    return gateway_load_config()


def connect(cfg):
    from everquill_gateway.cli import connect as gateway_connect
    return gateway_connect(cfg)


def _output_text(text, stream):
    """Escape only characters the destination cannot encode, without changing it."""
    encoding = getattr(stream, "encoding", None)
    return text.encode(encoding, "backslashreplace").decode(encoding) if encoding else text


def print(*values, sep=" ", end="\n", file=None, flush=False):
    stream = sys.stdout if file is None else file
    with io.StringIO() as buffer:
        builtins.print(*values, sep=sep, end=end, file=buffer)
        text = _output_text(buffer.getvalue(), stream)
    builtins.print(text, end="", file=stream, flush=flush)


class _ArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            stream = file or sys.stderr
            super()._print_message(_output_text(message, stream), stream)


SEARCH_WORDS = (
    "cari",
    "search",
    "find",
    "grep",
    "dimana",
    "di mana",
    "where",
)


def _proposal_target(root, file):
    relative = PureWindowsPath(file)
    if relative.is_absolute() or relative.drive:
        raise ValueError("absolute paths are not allowed")
    if ".." in relative.parts:
        raise ValueError("parent path segments are not allowed")

    target = (root / Path(*relative.parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes project root") from exc
    return target


def _create_target(root, file, *, allow_create_dirs=False):
    """Check each lexical component before resolving a new file target."""
    _safe_path(root, file)
    relative = PureWindowsPath(file)
    current = root
    missing = []
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("symlink or junction target is not allowed")
        if part == relative.parts[-1] and current == root.joinpath(*relative.parts):
            break
        if current.exists():
            if not current.is_dir() or missing:
                raise ValueError("parent directory is not available")
        else:
            missing.append(current)
    target = _proposal_target(root, file)
    if target.suffix.lower() not in TEXT_SUFFIXES and target.name.lower() not in {"readme", "license", "makefile"}:
        raise ValueError("CREATE requires a recognized text filename")
    if missing and not allow_create_dirs:
        raise ValueError("parent directory does not exist; directory creation is disabled")
    if target.is_symlink() or target.exists() or getattr(target, "is_junction", lambda: False)():
        raise ValueError("CREATE target already exists")
    if target == root:
        raise ValueError("CREATE target must be a file")
    return target, missing


def _delete_target(root, file):
    relative = PureWindowsPath(file)
    if relative.is_absolute() or relative.drive or ".." in relative.parts or not relative.parts:
        raise ValueError("unsafe DELETE path")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("symlink or junction target is not allowed")
    return _safe_path(root, file)

def _create_bytes(proposal):
    content = proposal.get("content")
    if not isinstance(content, str) or proposal.get("edits"):
        raise ValueError("CREATE requires text content and no edits")
    if "\x00" in content or contains_sensitive_content(
            proposal.get("file", ""), content, proposal.get("reason", ""),
            "\n".join(proposal.get("changes", []))):
        raise ValueError("CREATE contains sensitive or non-text content")
    try:
        return content.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("CREATE content is not valid UTF-8") from exc

def _delete_bytes(proposal):
    content = proposal.get("old_content")
    if not isinstance(content, str) or proposal.get("edits"):
        raise ValueError("DELETE requires complete old_content and no edits")
    if "\x00" in content or contains_sensitive_content(
            proposal.get("file", ""), content, proposal.get("reason", ""),
            "\n".join(proposal.get("changes", []))):
        raise ValueError("DELETE contains sensitive or non-text content")
    try:
        return content.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("DELETE content is not valid UTF-8") from exc


def get_baseline(events):
    seqs = [
        event.get("seq", 0)
        for event in events
        if isinstance(event, dict) and isinstance(event.get("seq", 0), int)
    ]
    return max(seqs, default=0)


def event_text(event):
    payload = event.get("payload", {})

    if not isinstance(payload, dict):
        return ""

    return (
        payload.get("display_text")
        or payload.get("content")
        or ""
    )


def _history_events(history):
    """Accept only a scoped history list, not an ambiguous/missing response."""
    if not isinstance(history, dict) or not isinstance(history.get("chat_events"), list):
        raise ValueError("invalid chat history")
    events = history["chat_events"]
    if any(not isinstance(event, dict)
           or not isinstance(event.get("seq", 0), int)
           or isinstance(event.get("seq", 0), bool)
           or not isinstance(event.get("payload", {}), dict)
           or any(not isinstance(event.get("payload", {}).get(key), (str, type(None)))
                  for key in ("display_text", "content"))
           for event in events):
        raise ValueError("invalid chat event")
    return events


def _request_failure(exc, session_id=None, sent=False):
    """Classify failures without printing untrusted gateway or auth payloads."""
    from everquill.meta_backend import ProviderStatusError
    if (isinstance(exc, ProviderStatusError) and exc.status in (401, 403)) or (
            isinstance(exc, RuntimeError) and str(exc) == "backend authentication required: MODEL_API_KEY"):
        print("[everquill] Meta authentication required; configure MODEL_API_KEY in your environment.")
        return "auth_required"
    if isinstance(exc, ProviderStatusError):
        print("[everquill] Meta provider request failed; no automatic retry or provider fallback.")
        return "send_failed"
    if isinstance(exc, AuthError) or (isinstance(exc, GatewayError) and exc.status in (401, 403)):
        print("[everquill] Authentication required; run `everquill gateway auth export`.")
        return "auth_required"
    if isinstance(exc, GatewayError) and exc.status in (404, 410) and session_id:
        print("[everquill] Session unavailable. Check the session ID before retrying; no main-chat fallback.")
        return "session_unavailable"
    if isinstance(exc, ValueError) and str(exc) in ("invalid chat history", "invalid chat event"):
        print("[everquill] Invalid chat history response; no automatic retry.")
        return "invalid_response"
    if isinstance(exc, ValueError) and str(exc) == "invalid backend response":
        print("[everquill] Invalid backend response; no automatic retry.")
        return "invalid_response"
    if isinstance(exc, TimeoutError):
        print("[everquill] Gateway timed out; delivery is uncertain. No automatic retry.")
        return "timeout"
    if sent:
        print("[everquill] Chat failed; delivery is uncertain. The session may still exist. No automatic retry.")
    else:
        print("[everquill] Request failed; no automatic retry. Check connection and session before retrying.")
    return "send_failed"


def search_project(prompt):
    stopwords = {
        "cari", "search", "find", "grep",
        "jelaskan", "jelasin", "file", "files",
        "project", "ini", "itu", "yang", "dan",
        "di", "ke", "dari", "untuk", "dengan",
        "the", "this", "that", "in", "on", "of",
        "and", "show", "explain"
    }

    words = re.findall(r"[A-Za-z0-9_.-]+", prompt)

    keywords = [
        word for word in words
        if len(word) >= 4 and word.lower() not in stopwords
    ]

    results = []
    seen = set()

    for keyword in keywords:
        matches = grep_files(keyword, limit=30)

        for match in matches:
            key = (
                match["file"],
                match["line"]
            )

            if key in seen:
                continue

            seen.add(key)

            file_path = match["file"].lower()

            if file_path.endswith(".bak"):
                continue

            score = 0

            if file_path.startswith("src\\"):
                score += 100

            if file_path.endswith(".py"):
                score += 50

            if file_path.endswith(".md"):
                score -= 20

            if keyword.lower() in file_path:
                score += 100

            results.append({
                **match,
                "_score": score
            })

    results.sort(
        key=lambda item: item["_score"],
        reverse=True
    )

    for item in results:
        item.pop("_score", None)

    return results[:30]


def build_context(prompt, selected_files=None):
    stopwords = {
        "jelaskan", "jelasin", "bagaimana", "kenapa",
        "apa", "fungsi", "cara", "yang", "dan",
        "di", "dari", "untuk", "dengan", "ini",
        "itu", "project", "kode", "code",
        "how", "why", "what", "explain", "does",
        "work", "the", "this", "that", "and",
        "with", "from", "for"
    }

    words = re.findall(r"[A-Za-z0-9_.-]+", prompt)

    keywords = [
        word for word in words
        if len(word) >= 4 and word.lower() not in stopwords
    ]

    results = []
    seen = set()

    for keyword in keywords:
        for match in grep_files(keyword, limit=50):
            file_path = match["file"].replace("/", "\\")
            lower_path = file_path.lower()

            if lower_path.endswith(".bak"):
                continue

            key = (file_path, match["line"])

            if key in seen:
                continue

            seen.add(key)

            score = 0
            text_line = match["text"].lower()

            if lower_path.endswith(".py"):
                score += 100

            if keyword.lower() in lower_path:
                score += 100

            if keyword.lower() in text_line:
                score += 20

            if (
                text_line.lstrip().startswith("def ")
                or text_line.lstrip().startswith("class ")
            ):
                score += 100

            results.append({
                **match,
                "file": file_path,
                "_score": score
            })

    results.sort(
        key=lambda item: item["_score"],
        reverse=True
    )

    if not results:
        return build_project_context(
            root=str(Path.cwd()), selected_files=selected_files
        )[:12000]

    sections = [
        "TARGETED PROJECT CONTEXT:"
    ]

    for item in results[:20]:
        sections.append(
            f"{item['file']}:{item['line']} - {item['text']}"
        )
        if selected_files is not None and item["file"] not in selected_files:
            selected_files.append(item["file"])

    # Primary source files.
    files = []
    seen_files = set()

    for item in results:
        if item["file"] not in seen_files:
            seen_files.add(item["file"])
            files.append(item["file"])

        if len(files) >= 2:
            break

    # Follow local Python imports from the primary files.
    dependency_files = []

    for file_path in list(files):
        if not file_path.lower().endswith(".py"):
            continue

        try:
            source = read_file(
                file_path,
                max_chars=30000
            )

            for match in re.finditer(
                r"from\s+\.([A-Za-z0-9_]+)\s+import|"
                r"import\s+([A-Za-z0-9_]+)",
                source
            ):
                module = match.group(1) or match.group(2)

                candidate = (
                    Path(file_path).parent /
                    f"{module}.py"
                )

                candidate = candidate.as_posix()

                if Path(candidate).exists():
                    candidate = candidate.replace("/", "\\")
                    if candidate not in files and candidate not in dependency_files:
                        dependency_files.append(candidate)

        except Exception:
            pass

    files.extend(dependency_files[:2])

    # Read only relevant sections from each file.
    for file_path in files[:4]:
        try:
            full_text = read_file(
                file_path,
                max_chars=30000
            )

            lines = full_text.splitlines()

            relevant_lines = [
                item["line"]
                for item in results
                if item["file"] == file_path
            ]

            # For dependency files, include their public definitions.
            if file_path in dependency_files:
                for n, line in enumerate(lines, 1):
                    stripped = line.strip()

                    if (
                        stripped.startswith("def ")
                        or stripped.startswith("class ")
                    ):
                        relevant_lines.append(n)

            selected = set()

            for line_no in relevant_lines[:12]:
                start_line = max(1, line_no - 6)
                end_line = min(len(lines), line_no + 12)

                for n in range(start_line, end_line + 1):
                    selected.add(n)

            if selected:
                excerpt = "\n".join(
                    f"{n}: {lines[n - 1]}"
                    for n in sorted(selected)
                )

                sections.append(
                    f"\n--- {file_path} ---\n{excerpt}"
                )
                if selected_files is not None and file_path not in selected_files:
                    selected_files.append(file_path)

        except Exception:
            pass

    return "\n".join(sections)[:12000]


def parse_proposal(text):
    """Parse the entire Muse change set or reject it without dropping members."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    proposals = data.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("proposal response must contain a 'proposals' list")
    allowed_actions = {"MODIFY", "CREATE", "DELETE"}
    result = []
    for item in proposals:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ValueError("proposal contains an invalid target")
        file = item["file"].strip()
        action = str(item.get("action", "")).upper().strip()
        if not file or action not in allowed_actions:
            raise ValueError("proposal contains an invalid action or target")
        reason = str(item.get("reason", "")).strip()
        changes = item.get("changes", [])
        if not isinstance(changes, list):
            changes = [str(changes)]
        edits = item.get("edits", [])
        if not isinstance(edits, list):
            raise ValueError("proposal contains invalid edits")
        clean_edits = []
        for edit in edits:
            if (not isinstance(edit, dict) or
                    not isinstance(edit.get("old"), str) or
                    not isinstance(edit.get("new"), str)):
                raise ValueError("proposal contains an invalid edit")
            clean_edits.append({"old": edit["old"], "new": edit["new"]})
        result.append({
            "file": file,
            "action": action,
            "reason": reason,
            "changes": [str(change) for change in changes],
            "edits": clean_edits,
            **({"content": item.get("content")} if action == "CREATE" else {}),
            **({"old_content": item.get("old_content")} if action == "DELETE" else {}),
        })
    return result

def _apply_exact_edits(current, edits, file):
    """Reject ambiguous or overlapping blocks before computing new content."""
    if not isinstance(edits, list) or not edits:
        raise ValueError(f"{file}: no edits provided")

    # Project readers normalize CRLF. Match the on-disk style before checking
    # exact occurrences so Windows files retain their original line endings.
    if "\r\n" in current and "\n" not in current.replace("\r\n", ""):
        def match_newlines(value):
            return value.replace("\r\n", "\n").replace("\n", "\r\n")
    else:
        def match_newlines(value):
            return value

    def positions(text, block):
        found = []
        start = 0
        while (position := text.find(block, start)) != -1:
            found.append(position)
            start = position + 1
        return found

    normalized = []
    spans = []
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            raise ValueError(f"{file}: edit {index} is invalid")
        old, new = edit.get("old"), edit.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError(f"{file}: edit {index} is invalid")
        if not old:
            raise ValueError(f"{file}: edit {index} has empty old code")
        old, new = match_newlines(old), match_newlines(new)
        normalized.append((old, new))
        matches = positions(current, old)
        count = len(matches)
        if count != 1:
            raise ValueError(
                f"{file}: edit {index} old code occurs {count} times; expected exactly once"
            )
        start = matches[0]
        end = start + len(old)
        if any(start < other_end and other_start < end
               for other_start, other_end in spans):
            raise ValueError(f"{file}: edit {index} overlaps another edit")
        spans.append((start, end))

    updated = current
    for index, (old, new) in enumerate(normalized, 1):
        count = len(positions(updated, old))
        if count != 1:
            raise ValueError(
                f"{file}: edit {index} old code occurs {count} times after prior edits"
            )
        updated = updated.replace(old, new, 1)
    if updated == current:
        raise ValueError(f"{file}: edits do not change the file")
    return updated


def _file_signature(info):
    """Metadata that must remain stable between validation and replacement."""
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size,
            stat.S_IMODE(info.st_mode))


def _unchanged_file(target, original_bytes, signature):
    before = target.stat()
    if _file_signature(before) != signature:
        return False
    current = target.read_bytes()
    return current == original_bytes and _file_signature(target.stat()) == signature


def validate_proposal(proposals, root=None, snapshots=None, *, allow_create_dirs=False):
    """Validate targets and capture MODIFY bytes or CREATE absence."""
    root = Path(root or Path.cwd()).resolve()
    errors = []
    if snapshots is not None:
        snapshots.clear()
    print("[everquill] validating proposed edits...")
    if not proposals:
        print("[everquill] Proposal contains no changes.")
        print("[everquill] No files modified.")
        return False

    seen_targets = set()
    for proposal in proposals:
        file = proposal.get("file", "")
        action = proposal.get("action", "")
        edits = proposal.get("edits", [])
        if action not in ("MODIFY", "CREATE", "DELETE"):
            errors.append(f"{file}: unsupported action: {action}")
            print(f"✗ {file}")
            print(f"  unsupported action: {action}")
            continue
        try:
            if action == "CREATE":
                target, _ = _create_target(root, file, allow_create_dirs=allow_create_dirs)
                _create_bytes(proposal)
            elif action == "DELETE":
                target = _delete_target(root, file)
                _delete_bytes(proposal)
            else:
                target = _safe_path(root, file)
            if target in seen_targets:
                raise ValueError("target appears more than once in proposal")
            seen_targets.add(target)
        except ValueError as exc:
            errors.append(f"{file}: {exc}")
            print(f"✗ {file}")
            print(f"  {exc}")
            continue
        if action == "CREATE":
            if snapshots is not None:
                snapshots[file] = None
            print(f"✓ {file}")
            print("  new UTF-8 file; target absent")
            continue
        if not target.is_file():
            errors.append(f"{file}: file not found")
            print(f"✗ {file}")
            print("  file not found")
            continue
        try:
            before = target.stat()
            current_bytes = target.read_bytes()
            after = target.stat()
            if _file_signature(before) != _file_signature(after):
                raise ValueError("file changed during validation")
            current = current_bytes.decode("utf-8")
            if action == "DELETE":
                if current_bytes != _delete_bytes(proposal):
                    raise ValueError("old_content does not match the current file")
            else:
                _apply_exact_edits(current, edits, file)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{file}: {exc}")
            print(f"✗ {file}")
            print(f"  {exc}")
            continue
        if snapshots is not None:
            snapshots[file] = (hashlib.sha256(current_bytes).hexdigest(),
                               *_file_signature(after))
        print(f"✓ {file}")
        print("  complete old content matches" if action == "DELETE" else
              "  each old block occurs exactly once")
    if errors:
        print("\n[everquill] Proposal is NOT applicable.")
        print("[everquill] No files modified.")
        return False
    print("\n[everquill] Proposal is applicable.")
    return True

def build_diff(proposal):
    """Build a unified diff from Muse old/new edits."""
    file = proposal.get("file", "")
    edits = proposal.get("edits", [])

    if proposal.get("action") == "CREATE":
        try:
            content = _create_bytes(proposal).decode("utf-8")
        except ValueError:
            return ""
        return "\n".join(difflib.unified_diff(
            [], content.splitlines(), fromfile="/dev/null",
            tofile=f"b/{file}", lineterm="",
        ))

    if proposal.get("action") == "DELETE":
        try:
            content = _delete_bytes(proposal).decode("utf-8")
        except ValueError:
            return ""
        return "\n".join(difflib.unified_diff(
            content.splitlines(), [], fromfile=f"a/{file}",
            tofile="/dev/null", lineterm="",
        ))

    if not file or not isinstance(edits, list):
        return ""

    chunks = []

    for edit in edits:
        if not isinstance(edit, dict):
            continue

        old = edit.get("old")
        new = edit.get("new")

        if not isinstance(old, str) or not isinstance(new, str):
            continue

        diff = difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{file}",
            tofile=f"b/{file}",
            lineterm="",
        )

        chunks.extend(diff)

    return "\n".join(chunks)


def print_proposal(proposals):
    print("\n[everquill] PROPOSAL")
    print("[everquill] No files modified.\n")

    if not proposals:
        print("No proposed changes.")
        return

    diffs = []
    for index, proposal in enumerate(proposals, 1):
        print(f"{index}. {proposal['file']}")
        print(f"   [{proposal['action']}]")

        if proposal["reason"]:
            print(f"   Reason: {proposal['reason']}")

        if proposal["changes"]:
            print("   Changes:")

            for change in proposal["changes"]:
                print(f"   - {change}")

        diff = build_diff(proposal)
        if diff:
            diffs.append(diff)
        print()
    if diffs:
        print("   Diff:")
        for diff in diffs:
            for line in diff.splitlines():
                print(f"   {line}")
            print()


def _stage_bytes(target, content, *, create=False):
    """Write replacement bytes beside a target without changing the target."""
    descriptor, name = tempfile.mkstemp(
        prefix=".everquill-", suffix=".tmp", dir=target.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if not create:
            os.chmod(staged, stat.S_IMODE(target.stat().st_mode))
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _remove_created_dirs(directories):
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            break


def _rollback_deleted_file(entry, root):
    file, target, original_bytes, _, backup, identity = entry
    try:
        if (_delete_target(root, file) != target or target.is_symlink()
                or getattr(target, "is_junction", lambda: False)()):
            raise ValueError("deleted target changed since write")
        if ((backup.stat().st_dev, backup.stat().st_ino) != identity or
                backup.read_bytes() != original_bytes):
            raise ValueError("recovery copy changed since write")
        if target.exists():
            if ((target.stat().st_dev, target.stat().st_ino) != identity or
                    target.read_bytes() != original_bytes):
                raise ValueError("deleted target replaced since write")
        else:
            os.link(backup, target)
        backup.unlink()
    except (OSError, ValueError) as exc:
        print(f"[everquill] rollback cancelled: {file}: {exc}")
        print("[everquill] Review the target and recovery copy manually.")
        return False
    print(f"[everquill] restored deleted file: {file}")
    return True

def _rollback_target(root, file):
    target = _proposal_target(root, file)
    current = root
    for part in PureWindowsPath(file).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("path changed since write")
    return target

def _rollback_written_files(written, root, created_dirs=()):
    """Recover each owned target; preserve conflicts and report partial recovery."""
    complete = True
    for entry in reversed(written):
        file, target, original_bytes, written_digest, *identity = entry
        if written_digest is None:
            if not _rollback_deleted_file(entry, root):
                complete = False
            continue
        replacement = None
        try:
            if _rollback_target(root, file) != target or target.is_symlink():
                raise ValueError("path changed since write")
            if (not target.is_file() or
                    (identity and (target.stat().st_dev, target.stat().st_ino) != identity[0]) or
                    hashlib.sha256(target.read_bytes()).hexdigest() != written_digest):
                raise ValueError("target changed since write")
            if original_bytes is None:
                target.unlink()
                print(f"[everquill] removed created file: {file}")
            else:
                replacement = _stage_bytes(target, original_bytes)
                if (_rollback_target(root, file) != target or target.is_symlink() or
                        (identity and (target.stat().st_dev, target.stat().st_ino) != identity[0]) or
                        hashlib.sha256(target.read_bytes()).hexdigest() != written_digest):
                    raise ValueError("target changed during rollback")
                os.replace(replacement, target)
                print(f"[everquill] restored: {file}")
        except (OSError, ValueError) as exc:
            complete = False
            print(f"[everquill] rollback cancelled: {file}: {exc}")
            print("[everquill] Review this target manually.")
        finally:
            if replacement is not None:
                replacement.unlink(missing_ok=True)
    _remove_created_dirs(created_dirs)
    if complete:
        print("[everquill] All modified files were rolled back.")
    else:
        print("[everquill] Rollback incomplete; review conflicted targets.")
    return complete


def apply_proposal(proposals, root=None, expected_snapshots=None, *, approved=False,
                   allow_create_dirs=False, delete_confirmed=False):
    """Preflight an approved proposal, write, validate, and recover guarded targets."""
    if approved is not True:
        raise PermissionError("proposal requires explicit approval")
    root = Path(root or Path.cwd()).resolve()
    if not proposals:
        raise ValueError("proposal contains no changes")
    if any(p.get("action") == "DELETE" for p in proposals) and delete_confirmed is not True:
        raise PermissionError("DELETE requires separate explicit confirmation")

    prepared = []
    seen_targets = set()
    missing_dirs = {}
    original_identity = {}
    for proposal in proposals:
        file = proposal.get("file", "")
        action = proposal.get("action")
        if action not in ("MODIFY", "CREATE", "DELETE"):
            raise ValueError(f"{file}: unsupported action: {action}")
        try:
            if action == "CREATE":
                target, missing_dirs[file] = _create_target(
                    root, file, allow_create_dirs=allow_create_dirs)
            elif action == "DELETE":
                target = _delete_target(root, file)
            else:
                target = _safe_path(root, file)
        except ValueError as exc:
            if str(exc) == "non-UTF-8 project file":
                raise ValueError(f"{file}: file is not UTF-8") from exc
            raise ValueError(f"{file}: {exc}") from exc
        if target in seen_targets:
            raise ValueError(f"{file}: target appears more than once in proposal")
        seen_targets.add(target)
        if action == "CREATE":
            if (expected_snapshots is None or file not in expected_snapshots or
                    expected_snapshots[file] is not None):
                raise ValueError(f"{file}: proposal is stale; CREATE requires validation of absence")
            prepared.append((file, target, None, _create_bytes(proposal)))
            continue
        if not target.is_file():
            raise FileNotFoundError(file)
        before = target.stat()
        original_bytes = target.read_bytes()
        after = target.stat()
        identity = _file_signature(after)
        if _file_signature(before) != identity:
            raise ValueError(f"{file}: proposal is stale; file changed during preflight")
        original_identity[target] = identity
        if action == "DELETE" and (expected_snapshots is None or file not in expected_snapshots):
            raise ValueError(f"{file}: proposal is stale; DELETE requires validation")
        if expected_snapshots is not None:
            expected = expected_snapshots.get(file)
            actual_digest = hashlib.sha256(original_bytes).hexdigest()
            if (expected[0] if isinstance(expected, tuple) else expected) != actual_digest or (
                    isinstance(expected, tuple) and expected[1:] != identity[:len(expected) - 1]):
                raise ValueError(f"{file}: proposal is stale; file changed since validation")
        try:
            current = original_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"{file}: file is not UTF-8") from exc
        if action == "DELETE":
            if original_bytes != _delete_bytes(proposal):
                raise ValueError(f"{file}: old_content does not match the current file")
            prepared.append((file, target, original_bytes, None))
        else:
            updated = _apply_exact_edits(current, proposal.get("edits"), file)
            prepared.append((file, target, original_bytes, updated.encode("utf-8")))

    staged = []
    staged_by_file = {}
    written = []
    created_dirs = []
    pending_replace = None
    try:
        # Recheck every target before staging, creating directories, or writing.
        for file, target, original_bytes, updated_bytes in prepared:
            current = (_create_target(root, file, allow_create_dirs=allow_create_dirs)[0]
                       if original_bytes is None else
                       _delete_target(root, file) if updated_bytes is None else _safe_path(root, file))
            if current != target or (original_bytes is not None and
                                     not _unchanged_file(target, original_bytes,
                                                         original_identity[target])):
                raise ValueError(f"{file}: proposal is stale; file changed since validation")
        for file, directories in missing_dirs.items():
            for directory in directories:
                if directory not in created_dirs:
                    directory.mkdir()
                    created_dirs.append(directory)
        for file, target, original_bytes, updated_bytes in prepared:
            if updated_bytes is not None and updated_bytes != original_bytes:
                replacement = _stage_bytes(target, updated_bytes, create=original_bytes is None)
                staged.append((file, target, original_bytes, updated_bytes, replacement))
                staged_by_file[file] = replacement
        for file, target, original_bytes, updated_bytes in prepared:
            if original_bytes is None:
                if _create_target(root, file)[0] != target:
                    raise ValueError(f"{file}: proposal is stale; path changed since validation")
            elif ((_delete_target(root, file) if updated_bytes is None else _safe_path(root, file))
                  != target or not _unchanged_file(target, original_bytes,
                                                     original_identity[target])):
                raise ValueError(f"{file}: proposal is stale; file changed since validation")
        # Apply in proposal order so every operation participates in one change set.
        for file, target, original_bytes, updated_bytes in prepared:
            if updated_bytes is None:
                if (_delete_target(root, file) != target or
                        not _unchanged_file(target, original_bytes,
                                            original_identity[target])):
                    raise ValueError(f"{file}: file changed before delete")
                descriptor, name = tempfile.mkstemp(
                    prefix=".everquill-", suffix=".tmp", dir=target.parent)
                os.close(descriptor)
                backup = Path(name)
                backup.unlink()
                try:
                    os.link(target, backup)
                    identity = (backup.stat().st_dev, backup.stat().st_ino)
                    if (_delete_target(root, file) != target or
                            identity != original_identity[target][:2] or
                            not _unchanged_file(target, original_bytes,
                                                original_identity[target])):
                        raise ValueError(f"{file}: file changed before delete")
                except (OSError, ValueError):
                    backup.unlink(missing_ok=True)
                    raise
                written.append((file, target, original_bytes, None, backup, identity))
                target.unlink()
                print(f"[everquill] deleted: {file}")
                continue
            replacement = staged_by_file[file]
            if original_bytes is None:
                if _create_target(root, file)[0] != target:
                    raise ValueError(f"{file}: proposal is stale; path changed before write")
                identity = (replacement.stat().st_dev, replacement.stat().st_ino)
                os.link(replacement, target)
                written.append((file, target, None, hashlib.sha256(updated_bytes).hexdigest(), identity))
                print(f"[everquill] created: {file}")
                continue
            if (_safe_path(root, file) != target or
                    not _unchanged_file(target, original_bytes,
                                        original_identity[target])):
                raise ValueError(f"{file}: proposal is stale; file changed before write")
            identity = (replacement.stat().st_dev, replacement.stat().st_ino)
            pending_replace = (file, target, original_bytes,
                               hashlib.sha256(updated_bytes).hexdigest(), identity)
            os.replace(replacement, target)
            written.append(pending_replace)
            pending_replace = None
            print(f"[everquill] modified: {file}")
    except (OSError, ValueError) as exc:
        if pending_replace is not None:
            file, target, original_bytes, digest, identity = pending_replace
            try:
                if (not target.is_symlink() and
                        (target.stat().st_dev, target.stat().st_ino) == identity and
                        hashlib.sha256(target.read_bytes()).hexdigest() == digest):
                    written.append(pending_replace)
            except OSError:
                pass
        for file, target, original_bytes, updated_bytes, replacement in staged:
            if not any(item[1] == target for item in written):
                try:
                    staged_id = (replacement.stat().st_dev, replacement.stat().st_ino)
                    target_id = (target.stat().st_dev, target.stat().st_ino)
                    if (not target.is_symlink() and staged_id == target_id and
                            hashlib.sha256(target.read_bytes()).hexdigest() ==
                            hashlib.sha256(updated_bytes).hexdigest()):
                        written.append((file, target, original_bytes,
                                        hashlib.sha256(updated_bytes).hexdigest(), staged_id))
                except OSError:
                    pass
        for file, target, original_bytes, updated_bytes, replacement in staged:
            replacement.unlink(missing_ok=True)
        if written:
            print(f"[everquill] Write failed: {exc}; rolling back...")
            if not _rollback_written_files(written, root, created_dirs):
                raise RuntimeError(
                    f"write failed: {exc}; rollback incomplete; review modified files"
                ) from exc
            raise ValueError(f"write failed: {exc}; all modified files restored") from exc
        _remove_created_dirs(created_dirs)
        raise
    finally:
        for file, target, original_bytes, updated_bytes, replacement in staged:
            replacement.unlink(missing_ok=True)

    python_files = [(file, target) for file, target, _, digest, *_ in written
                    if digest is not None and target.suffix.lower() == ".py"]
    validation_errors = []
    for file, target in python_files:
        try:
            compile(target.read_text(encoding="utf-8"), str(target), "exec")
        except (OSError, UnicodeError, SyntaxError) as exc:
            validation_errors.append((file, exc))
    if validation_errors:
        for file, exc in validation_errors:
            print(f"[everquill] post-write syntax validation failed: {file}: {exc}")
        print("[everquill] rolling back modified files...")
        _rollback_written_files(written, root, created_dirs)
        return False
    print(f"[everquill] syntax validation passed ({len(python_files)} Python file(s)).")
    print("[everquill] running project tests...")
    try:
        result = run_project_tests(
            root, expected_targets=[entry[0] for entry in written]
        )
    except Exception as exc:
        print(f"[everquill] project test execution failed: {exc}")
        _rollback_written_files(written, root, created_dirs)
        return False
    discovery = getattr(result, "discovery", None)
    if discovery is not None and discovery.status == "DISCOVERED":
        print(f"[everquill] test discovery: FOUND {discovery.language} / "
              f"{discovery.framework}")
    if result.command:
        print(f"[everquill] test command: {result.command}")
    print(f"[everquill] test status: {result.status}")
    print(f"[everquill] test isolation: {getattr(result, 'isolation', 'NOT_REPORTED')}")
    if getattr(result, "state_cleanup", None) in {"RETAINED", "DEFERRED"}:
        print(f"[everquill] retained execution state: {ascii(result.execution_state)}")
    if result.output:
        print(result.output)
    if result.status == "TERMINATION_FAILED":
        print("[everquill] Process cleanup is incomplete; rollback was not attempted. "
              "Inspect the project before retrying.")
        return False
    if not result.passed:
        print("[everquill] project tests failed; rolling back modified files...")
        _rollback_written_files(written, root, created_dirs)
        return False
    print("[everquill] project tests passed; proposal evaluated successfully.")
    for file, target, original_bytes, digest, *extra in written:
        if digest is None:
            extra[0].unlink(missing_ok=True)
    return True

def request_approval():
    """Ask the user to explicitly approve an applicable proposal."""
    try:
        answer = input("\n[everquill] Apply this proposal? [y/N]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[everquill] Approval cancelled.")
        return False

    if answer in {"y", "yes"}:
        print("[everquill] Proposal approved.")
        return True

    print("[everquill] Proposal rejected.")
    print("[everquill] No files modified.")
    return False


def request_delete_approval(file):
    """Confirm one deletion by typing its relative target."""
    try:
        return input(f"[everquill] Type DELETE {file} to confirm: ") == f"DELETE {file}"
    except (EOFError, KeyboardInterrupt):
        return False

def process_proposal_reply(reply, approve=request_approval, *, allow_create_dirs=False,
                           confirm_delete=None):
    try:
        proposals = parse_proposal(reply)
    except Exception:
        print("[everquill] Invalid proposal response; raw response withheld.")
        return "invalid"

    if any(p.get("action") == "CREATE" and
           contains_sensitive_content(
               p["file"], p.get("content") if isinstance(p.get("content"), str) else "",
               p.get("reason", ""), "\n".join(p.get("changes", [])))
           for p in proposals):
        print("[everquill] Sensitive CREATE proposal rejected; no files modified.")
        return "not_applicable"
    if any(p.get("action") == "DELETE" and
           contains_sensitive_content(
               p["file"], p.get("old_content") if isinstance(p.get("old_content"), str) else "",
               p.get("reason", ""), "\n".join(p.get("changes", [])))
           for p in proposals):
        print("[everquill] Sensitive DELETE proposal rejected; no files modified.")
        return "not_applicable"
    if any(p.get("action") == "MODIFY" and contains_sensitive_content(
            p["file"], p.get("reason", ""), "\n".join(p.get("changes", [])),
            "\n".join(e.get("old", "") + "\n" + e.get("new", "")
                      for e in p.get("edits", []))) for p in proposals):
        print("[everquill] Sensitive MODIFY proposal rejected; no files modified.")
        return "not_applicable"

    try:
        for item in proposals:
            if item.get("action") in ("CREATE", "DELETE", "MODIFY"):
                (_delete_target if item.get("action") == "DELETE" else _safe_path)(
                    Path.cwd(), item["file"])
    except (ValueError, OSError):
        print("[everquill] Private or unsafe path rejected; no files modified.")
        return "not_applicable"

    print_proposal(proposals)
    snapshots = {}
    applicable = validate_proposal(
        proposals,
        snapshots=snapshots,
        allow_create_dirs=allow_create_dirs,
    )
    if not applicable:
        return "no_changes" if not proposals else "not_applicable"

    print("[everquill] Tests use limited isolation: reduced environment and redirected "
          "home/temp defaults; filesystem, network, and account permissions are NOT isolated.")
    if not approve():
        return "rejected"
    for item in proposals:
        if item.get("action") == "DELETE" and (
                confirm_delete is None or not confirm_delete(item["file"])):
            print("[everquill] DELETE confirmation declined; no files modified.")
            return "rejected"

    try:
        applied = apply_proposal(
            proposals,
            expected_snapshots=snapshots,
            approved=True,
            allow_create_dirs=allow_create_dirs,
            delete_confirmed=any(p.get("action") == "DELETE" for p in proposals),
        )
    except Exception as exc:
        if ("stale" in str(exc).lower() or
                "CREATE target already exists" in str(exc)):
            print("[everquill] Stale/conflict detected; change set not applied. Review project state before retrying.")
        else:
            print("[everquill] Write failed; review project state before retrying.")
        if "rollback incomplete" in str(exc):
            print("[everquill] Review modified files before retrying.")
        return "write_failed"

    if applied:
        print("[everquill] Proposal applied.")
        return "applied"

    print("[everquill] Proposal failed post-write checks.")
    return "validation_failed"


def request_context_approval(summary):
    if summary.get("purpose") == "tool_execution":
        print("[everquill] Read-only recovery execution preview; no result send is approved here.")
        for name in summary["files"]:
            print(f"  {ascii(name)}")
        try:
            return input("[everquill] Execute read-only recovery tools? [y/N]: ").strip() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False
    print(f"[everquill] Context preview: {summary['characters']} characters; "
          f"{len(summary['files'])} candidate file(s).")
    for name in summary["files"]:
        print(f"  {ascii(name)}")
    print("[everquill] Your request and the project label will also be sent.")
    try:
        provider = "Muse.ai" if os.environ.get("EVERQUILL_PROVIDER", "meta") == "muse" else "Meta"
        return input(f"[everquill] Send to {provider}? [y/N]: ").strip() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _make_reasoning_backend(cancel_event=None, *, provider=None, transport=None):
    provider = provider or os.environ.get("EVERQUILL_PROVIDER", "meta")
    if provider == "muse":
        try:
            from everquill.muse_backend import MuseReasoningBackend
        except ModuleNotFoundError as exc:
            if exc.name != "everquill.muse_backend":
                raise
            raise ValueError("muse provider is unsupported in this Meta-only distribution") from None
        return MuseReasoningBackend(load_config, connect, cancel_event=cancel_event)
    if provider != "meta":
        raise ValueError("unsupported reasoning provider; choose meta or muse")
    from everquill.meta_backend import MetaModelBackend, https_transport
    backend = MetaModelBackend(transport=transport or https_transport,
                               api_key=os.environ.get("MODEL_API_KEY"))
    if cancel_event is not None:
        backend._cancel_event = cancel_event
    return backend


def _receive_reply(backend, confirm_context, cancel_event, *, pending_tool=None):
    """Journal read-only calls before each irreversible recovery boundary."""
    from everquill.meta_backend import MetaModelBackend
    from everquill.reply import BackendReply, ToolCallRequest, ToolResult
    from everquill.session import save_provider_session

    def persist(pending):
        save_provider_session(Path.cwd(), "meta", pending["response_id"], pending_tool=pending)

    recovering = pending_tool is not None
    if recovering:
        if pending_tool["phase"] not in {"prepared", "results_ready"}:
            raise RuntimeError("pending tool outcome uncertain; manual review required; no automatic retry")
        reply = BackendReply("tool_request", tool_calls=tuple(
            ToolCallRequest(call["name"], call["arguments"], call["call_id"])
            for call in pending_tool["calls"]), response_id=pending_tool["response_id"])
    else:
        reply = backend.stream_response()
    if not isinstance(backend, MetaModelBackend):
        return require_final_reply(reply)
    start_turn = pending_tool["turn"] if recovering else 0
    for turn in range(start_turn, 3):
        if not isinstance(reply, BackendReply):
            raise ValueError("invalid backend response")
        if reply.status != "tool_request":
            final = require_final_reply(reply)
            if pending_tool is not None and final.status == "complete":
                if not final.response_id:
                    raise ValueError("missing continuation response ID")
                save_provider_session(Path.cwd(), "meta", final.response_id)
            return final
        if turn == 2 or len(reply.tool_calls) > 8:
            raise ValueError("read tool turn limit reached")
        files = []
        for call in reply.tool_calls:
            if call.name != "read_file" or set(call.arguments) != {"path"}:
                raise ValueError("unsupported local tool")
            path = call.arguments["path"]
            if not isinstance(path, str) or contains_sensitive_content(path):
                raise ValueError("invalid local tool path")
            files.append(path)
        if pending_tool is None:
            if not reply.response_id:
                raise ValueError("missing tool response ID")
            pending_tool = {"version": 1, "provider": "meta", "response_id": reply.response_id,
                            "phase": "prepared", "turn": turn,
                            "calls": [{"call_id": call.call_id, "name": call.name,
                                       "arguments": dict(call.arguments)} for call in reply.tool_calls],
                            "results": []}
            persist(pending_tool)
        if cancel_event is not None and cancel_event.is_set():
            return BackendReply("cancelled")
        if pending_tool["phase"] == "prepared":
            if recovering:
                print("[everquill] Recovery: approve read-only tool execution before any read.")
                if not confirm_context({"files": files, "characters": 0, "purpose": "tool_execution"}):
                    return BackendReply("cancelled")
            if cancel_event is not None and cancel_event.is_set():
                return BackendReply("cancelled")
            pending_tool = {**pending_tool, "phase": "executing"}
            persist(pending_tool)
            results = []
            for call in reply.tool_calls:
                text = read_file(call.arguments["path"], root=Path.cwd(), max_chars=12000)
                if contains_sensitive_content(text):
                    raise ValueError("sensitive tool result")
                results.append(ToolResult(call.call_id, text))
            if sum(len(result.output) for result in results) > 12000:
                raise ValueError("tool context limit exceeded")
            pending_tool = {**pending_tool, "phase": "results_ready",
                            "results": [{"call_id": result.call_id, "output": result.output}
                                        for result in results]}
            persist(pending_tool)
        results = [ToolResult(result["call_id"], result["output"]) for result in pending_tool["results"]]
        size = sum(len(result.output) for result in results)
        if size > 12000 or any(contains_sensitive_content(result.output) for result in results):
            raise ValueError("invalid persisted tool results")
        if cancel_event is not None and cancel_event.is_set():
            return BackendReply("cancelled")
        print("[everquill] Approve sending the tool results; this does not execute tools.")
        if not confirm_context({"files": files, "characters": size, "purpose": "tool_results"}):
            return BackendReply("cancelled")
        if cancel_event is not None and cancel_event.is_set():
            return BackendReply("cancelled")
        pending_tool = {**pending_tool, "phase": "continuation_started"}
        persist(pending_tool)
        metadata = {"session_id": reply.response_id,
                    "pending_call_ids": [call.call_id for call in reply.tool_calls]} if recovering else None
        backend.send_message(results, metadata=metadata)
        reply = backend.stream_response()
        pending_tool = None if reply.status == "tool_request" else pending_tool
        recovering = False
    raise ValueError("read tool turn limit reached")


def ask_muse(
    prompt, proposal=False, session_id=None,
    approve=request_approval, cancel_event=None,
    confirm_context=request_context_approval, session_state=None,
):
    backend = None

    try:
        from everquill.session import load_provider_session
        provider = os.environ.get("EVERQUILL_PROVIDER", "meta")
        record = load_provider_session(Path.cwd(), provider)
        if record and "pending_tool" in record:
            if session_state is not None:
                session_id = session_state.get("id")
            if session_id != record["session_id"]:
                print("[everquill] Pending tools require --continue; nothing sent or executed.")
                return "recovery_required"
            if record["pending_tool"]["phase"] not in {"prepared", "results_ready"}:
                print("[everquill] Pending tool execution or continuation delivery is uncertain; "
                      "manual review required. Nothing repeated.")
                return "recovery_required"
            backend = _make_reasoning_backend(cancel_event)
            response = _receive_reply(backend, confirm_context, cancel_event,
                                      pending_tool=record["pending_tool"])
            if response.status != "complete":
                return response.status
            if session_state is not None:
                session_state["id"] = response.response_id
            print("\n" + response.text)
            return "answered"
        print("[everquill] building project context...")

        selected_files = []
        context = build_context(prompt, selected_files=selected_files)
        if contains_sensitive_content(context, prompt, Path.cwd().name,
                                      "\n".join(selected_files)):
            print("[everquill] Sensitive content detected in request context; "
                  "nothing sent. Remove it from the request or project copy.")
            return "sensitive_context"

        if cancel_event is not None and cancel_event.is_set():
            print("[everquill] cancelled.")
            return "cancelled"
        summary = {"files": selected_files, "characters": len(context)}
        if not confirm_context(summary):
            print("[everquill] context send declined; no project context sent.")
            return "cancelled"
        if cancel_event is not None and cancel_event.is_set():
            print("[everquill] cancelled.")
            return "cancelled"
        if session_state is not None:
            if session_state.get("pending"):
                if os.environ.get("EVERQUILL_PROVIDER", "meta") == "muse":
                    session_state["id"] = start_muse_session(Path.cwd())
                else:
                    session_state["id"] = None
                session_state["pending"] = False
            session_id = session_state.get("id")
        if proposal:
            instruction = f"""You are Everquill operating in PROPOSAL mode.

PROJECT:
{Path.cwd().name}

PROJECT CONTEXT:
{context}

USER REQUEST:
{prompt}

Do NOT modify files.
Do NOT run commands.

Analyze the request and return ONLY valid JSON.

Required schema:
{{
  "proposals": [
    {{
      "file": "relative/path/to/file",
      "action": "MODIFY",
      "reason": "short reason",
      "changes": [
        "specific proposed change"
      ],
      "edits": [
        {{
          "old": "exact existing code",
          "new": "exact proposed code"
        }}
      ]
    }}
  ]
}}

For a new UTF-8 text file, use this CREATE member schema:
{{"proposals": [{{"file": "relative/new.py", "action": "CREATE",
  "reason": "short reason", "changes": ["describe new file"],
  "content": "complete new file text"}}]}}

For an existing UTF-8 text file deletion, use this DELETE member schema:
{{"proposals": [{{"file": "relative/old.py", "action": "DELETE",
  "reason": "short reason", "changes": ["describe deletion"],
  "old_content": "complete exact existing file text"}}]}}

Allowed actions:
MODIFY
CREATE
DELETE

Rules:
- Return one change set in the "proposals" array, with multiple files when
  needed. All members share one review, approval, preflight, and validation.
- Use paths relative to the project root.
- Only propose changes supported by the supplied project context.
- Be specific.
- The "edits" field is required for MODIFY actions.
- CREATE requires complete "content" and no edits; one CREATE member may be
  combined with MODIFY and DELETE members when the task requires it.
- CREATE needs an existing parent directory; never propose private,
  hidden, Git-ignored, or credential-containing files.
- DELETE requires complete exact "old_content" and no edits; one DELETE member
  may be combined with MODIFY and CREATE. Do not propose private or ignored files.
- Do not repeat a target within the same change set.
- Each edit must contain exact existing "old" code and proposed "new" code.
- Each "old" block must match the supplied project context exactly and occur only once in its file.
- Do not invent code that is not supported by the project context.
- Do not include markdown fences.
- Do not include explanations outside the JSON.
"""
        else:
            instruction = f"""You are Everquill, a coding agent working in this project.

PROJECT:
{Path.cwd().name}

PROJECT CONTEXT:
{context}

USER REQUEST:
{prompt}

Use the supplied project context.
Do not invent files or code.
This is READ-ONLY mode.
Do not modify files or run commands.

Give a concise but complete answer.
When referring to code, include the file path.
"""

        backend = _make_reasoning_backend(cancel_event)
        from everquill.meta_backend import MetaModelBackend
        tools = [{'type': 'function', 'name': 'read_file',
                  'description': 'Read an admitted project text file; never write.',
                  'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
                                 'required': ['path'], 'additionalProperties': False}}] if isinstance(backend, MetaModelBackend) else None
        backend.send_message(
            [{"role": "user", "content": instruction}],
            tools=tools,
            metadata={"session_id": session_id} if session_id else None,
        )
        print("[everquill] sent")
        provider_label = "Meta" if isinstance(backend, MetaModelBackend) else "Muse.ai"
        print(f"[everquill] waiting for {provider_label}...")
        response = _receive_reply(backend, confirm_context, cancel_event)
        if response.status == "cancelled":
            print("[everquill] cancelled locally; the remote request may continue.")
            return "cancelled"
        if response.status == "timeout":
            print("[everquill] timeout waiting for a stable reply; remote processing may continue.")
            return "timeout"
        if response.status != "complete" or not response.text:
            raise ValueError("invalid backend response")
        if response.response_id:
            from everquill.session import save_provider_session
            if session_state is not None:
                session_state["id"] = response.response_id
            try:
                save_provider_session(Path.cwd(), "meta", response.response_id)
            except (OSError, UnicodeError, ValueError, AttributeError, TypeError):
                print("[everquill] Reply received but continuation not saved; resume after exit may fail.")
                return "session_save_failed"
        if proposal:
            return process_proposal_reply(
                response.text, approve=approve,
                confirm_delete=request_delete_approval
                if approve is request_approval else None)
        print("\n" + response.text)
        return "answered"

    except KeyboardInterrupt:
        print("\n[everquill] cancelled locally; the remote request may continue if already sent.")
        return "cancelled"
    except (AuthError, GatewayError, TimeoutError, ValueError, OSError, RuntimeError) as exc:
        return _request_failure(exc, session_id=session_id,
                                sent=backend is not None and getattr(backend, "connected", False) and session_id is not None)
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass


def run_agent_loop(
    prompt, proposal=False, session_id=None, iterate=False,
    approve=request_approval, cancel_event=None,
    confirm_context=request_context_approval, session_state=None,
):
    status = ask_muse(
        prompt,
        proposal=proposal,
        session_id=session_id,
        approve=approve,
        cancel_event=cancel_event,
        confirm_context=confirm_context,
        session_state=session_state,
    )

    if not iterate or not proposal:
        return status

    for iteration in range(2, 4):
        if status != "applied" or (
            cancel_event is not None and cancel_event.is_set()
        ):
            break

        print(f"[everquill] inspecting result (iteration {iteration}/3)...")
        inspection_prompt = f"""Inspect the current project after applying a proposal.

Original request:
{prompt}

Check whether the current code fully satisfies the request. If it does, return
{{"proposals": []}}. If a correction is required, return a minimal MODIFY
proposal using exact code from the current project context. Every correction
will require separate user approval."""
        status = ask_muse(
            inspection_prompt,
            proposal=True,
            session_id=session_id,
            approve=approve,
            cancel_event=cancel_event,
            confirm_context=confirm_context,
            session_state=session_state,
        )

    if status == "applied":
        print("[everquill] iteration limit reached (3).")

    return status


def start_muse_session(project_root):
    backend = _make_reasoning_backend()
    try:
        session_id = backend.start_session(Path(project_root).name)
    finally:
        backend.close()

    try:
        save_session(project_root, session_id)
    except (OSError, UnicodeError, ValueError):
        print("[everquill] Session created but local state not saved; resume after exit may fail.")
    return session_id


def _one_shot_exit_code(status):
    if status == "cancelled":
        return 130
    return 0 if status in {"answered", "applied", "no_changes"} else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "gateway":
        try:
            from everquill_gateway.cli import main as gateway_main
        except ModuleNotFoundError as exc:
            if exc.name != "everquill_gateway.cli":
                raise
            print("[everquill] gateway is unsupported in this Meta-only distribution.",
                  file=sys.stderr)
            return 2

        return gateway_main(sys.argv[2:])

    parser = _ArgumentParser(
        prog="everquill",
        description="Everquill coding agent"
    )

    parser.add_argument(
        "prompt",
        nargs="*",
        help="coding request"
    )

    parser.add_argument(
        "--proposal",
        action="store_true",
        help="generate a change proposal and ask before applying it"
    )
    parser.add_argument(
        "--iterate",
        action="store_true",
        help="propose, apply, and inspect results for up to 3 iterations"
    )
    tui_group = parser.add_mutually_exclusive_group()
    tui_group.add_argument(
        "--tui",
        action="store_true",
        help="open the interactive terminal interface"
    )
    tui_group.add_argument(
        "--no-tui",
        action="store_true",
        help="use the classic line-by-line prompt"
    )

    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--session",
        action="store_true",
        help="start a new persistent Everquill coding session"
    )
    session_group.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="continue the last Everquill coding session for this project"
    )

    parser.add_argument("--provider", choices=("meta", "muse"),
                        default=os.environ.get("EVERQUILL_PROVIDER", "meta"),
                        help="reasoning provider (default: meta; no fallback)")
    from everquill_gateway import __version__
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = parser.parse_args()
    os.environ["EVERQUILL_PROVIDER"] = args.provider

    if args.provider == "muse":
        try:
            from everquill.muse_backend import MuseReasoningBackend
        except ModuleNotFoundError as exc:
            if exc.name != "everquill.muse_backend":
                raise
            parser.error("muse provider is unsupported in this Meta-only distribution")

    if args.tui and args.prompt:
        parser.error("--tui opens an interactive session; omit the prompt")

    project_root = Path.cwd().resolve()
    session_id = None

    if args.session:
        print("[everquill] new session pending until context approval.")
    elif args.continue_session:
        if args.provider == "muse":
            try:
                session_id = load_session(project_root)
            except (OSError, ValueError, AttributeError, TypeError, RuntimeError):
                parser.error("session recovery metadata invalid; manual review required; nothing repeated")
        else:
            from everquill.session import load_provider_session, SessionRecoveryError
            try:
                record = load_provider_session(project_root, args.provider)
            except SessionRecoveryError:
                parser.error("session recovery metadata invalid; manual review required; nothing repeated")
            session_id = record["session_id"] if record else None
        if not session_id:
            parser.error(
                "no saved session for this project; run everquill --session first"
            )
        print(f"[everquill] continuing session: {session_id}")

    session_state = {"id": session_id, "pending": args.session}

    if args.continue_session and args.provider == "meta" and record and "pending_tool" in record:
        status = run_agent_loop("", session_id=session_id, session_state=session_state)
        return _one_shot_exit_code(status)

    if not args.prompt:
        if args.tui or (
            not args.no_tui and sys.stdin.isatty() and sys.stdout.isatty()
        ):
            from everquill.node_ui import run_node_tui

            mode = "iterate" if args.iterate else "proposal" if args.proposal else "ask"
            try:
                return_code = run_node_tui(project_root, session_id, mode,
                                           new_session=args.session)
            except RuntimeError as exc:
                if args.tui:
                    parser.error(str(exc))
                print(f"[everquill] {exc}; using classic prompt.")
            else:
                if return_code:
                    raise SystemExit(return_code)
                return

        print("Everquill")
        print("Type your coding request. Ctrl+C to exit.\n")

        while True:
            try:
                prompt = input("everquill> ").strip()

                if prompt:
                    run_agent_loop(
                        prompt,
                        proposal=args.proposal or args.iterate,
                        session_id=session_id,
                        iterate=args.iterate,
                        session_state=session_state,
                    )

            except (KeyboardInterrupt, EOFError):
                print("\nBye.")
                break

            except Exception:
                print("[everquill] Request failed. Check session and connection; no automatic retry.")

    else:
        try:
            status = run_agent_loop(
                " ".join(args.prompt),
                proposal=args.proposal or args.iterate,
                session_id=session_id,
                iterate=args.iterate,
                session_state=session_state,
            )
            return _one_shot_exit_code(status)
        except Exception:
            print("[everquill] Request failed. Check session and connection; no automatic retry.", file=sys.stderr)
            raise SystemExit(1) from None


if __name__ == "__main__":
    raise SystemExit(main())

