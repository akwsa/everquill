import json
import os
import time
from pathlib import Path


from everquill_gateway.migration import migrate_file, write_text_atomic


SESSION_FILE = Path.home() / ".config" / "everquill" / "sessions.json"
LEGACY_SESSION_FILE = Path.home() / ".config" / "muse-cli" / "muse-code-sessions.json"


def _migrate_legacy_session():
    return migrate_file(LEGACY_SESSION_FILE, SESSION_FILE)


def _project_key(project_root):
    return os.path.normcase(str(Path(project_root).resolve()))


def load_session(project_root, state_file=None):
    path = Path(state_file or SESSION_FILE)
    if state_file is None:
        _migrate_legacy_session()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None

    record = data.get("projects", {}).get(_project_key(project_root), {})
    session_id = record.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def save_session(project_root, session_id, state_file=None):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")

    path = Path(state_file or SESSION_FILE)
    if state_file is None:
        _migrate_legacy_session()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        data = {}

    projects = data.setdefault("projects", {})
    projects[_project_key(project_root)] = {
        "session_id": session_id,
        "updated_at": int(time.time()),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


class SessionRecoveryError(RuntimeError):
    def __init__(self):
        super().__init__("Session recovery metadata invalid; manual review required; nothing repeated")


def _validate_pending_tool(record, provider):
    if "pending_tool" not in record:
        return
    pending = record["pending_tool"]
    if (not isinstance(pending, dict)
            or set(pending) != {"version", "provider", "response_id", "phase", "turn", "calls", "results"}
            or type(pending["version"]) is not int or pending["version"] != 1
            or record.get("provider") != provider or pending["provider"] != provider
            or pending["response_id"] != record["session_id"]
            or pending["phase"] not in {"prepared", "executing", "results_ready", "continuation_started"}
            or type(pending["turn"]) is not int or pending["turn"] not in (0, 1)
            or not isinstance(pending["calls"], list) or not 1 <= len(pending["calls"]) <= 8
            or not isinstance(pending["results"], list)):
        raise SessionRecoveryError()
    ids = []
    for call in pending["calls"]:
        if (not isinstance(call, dict) or set(call) != {"call_id", "name", "arguments"}
                or not isinstance(call["call_id"], str) or not call["call_id"]
                or call["call_id"] in ids or not isinstance(call["name"], str) or not call["name"]
                or not isinstance(call["arguments"], dict)):
            raise SessionRecoveryError()
        ids.append(call["call_id"])
    result_ids, size = [], 0
    for result in pending["results"]:
        if (not isinstance(result, dict) or set(result) != {"call_id", "output"}
                or not isinstance(result["call_id"], str) or result["call_id"] not in ids
                or result["call_id"] in result_ids or not isinstance(result["output"], str)):
            raise SessionRecoveryError()
        result_ids.append(result["call_id"])
        size += len(result["output"])
    if (size > 12000 or (pending["phase"] in {"prepared", "executing"} and result_ids)
            or (pending["phase"] in {"results_ready", "continuation_started"} and set(result_ids) != set(ids))):
        raise SessionRecoveryError()


def _read_provider_state(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate session key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite session value")

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError):
        raise SessionRecoveryError() from None
    if not isinstance(data, dict):
        raise SessionRecoveryError()
    return data


def load_provider_session(project_root, provider, state_file=None):
    path = Path(state_file or SESSION_FILE)
    data = _read_provider_state(path)
    try:
        record = data.get("providers", {}).get(provider, {}).get(_project_key(project_root))
        if record is not None:
            if not isinstance(record, dict) or not isinstance(record.get("session_id"), str) or not record["session_id"]:
                raise SessionRecoveryError()
            _validate_pending_tool(record, provider)
            return record
    except (AttributeError, TypeError):
        raise SessionRecoveryError() from None
    if provider == "muse":
        session_id = load_session(project_root, state_file=state_file)
        if session_id:
            return {"session_id": session_id, "provider": "muse"}
    return None


def save_provider_session(project_root, provider, session_id, state_file=None, *, pending_tool=None):
    if provider not in {"meta", "muse"} or not isinstance(session_id, str) or not session_id:
        raise ValueError("invalid provider session")
    path = Path(state_file or SESSION_FILE)
    data = _read_provider_state(path)
    record = {"provider": provider, "session_id": session_id, "updated_at": int(time.time())}
    if pending_tool is not None:
        record["pending_tool"] = pending_tool
        _validate_pending_tool(record, provider)
    data.setdefault("providers", {}).setdefault(provider, {})[_project_key(project_root)] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def extract_session_id(value):
    if isinstance(value, dict):
        session_id = value.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        for nested in value.values():
            session_id = extract_session_id(nested)
            if session_id:
                return session_id
    elif isinstance(value, list):
        for nested in value:
            session_id = extract_session_id(nested)
            if session_id:
                return session_id
    return None
