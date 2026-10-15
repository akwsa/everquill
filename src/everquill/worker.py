"""JSON-lines bridge between the Node.js terminal UI and Everquill."""

import contextlib
import json
import queue
import sys
import threading
from pathlib import Path

from everquill.cli import run_agent_loop
from everquill.session import load_session


_emit_lock = threading.Lock()


def emit(event, **fields):
    payload = json.dumps({"type": event, **fields}, ensure_ascii=False)
    with _emit_lock:
        sys.__stdout__.write(payload + "\n")
        sys.__stdout__.flush()


class EventWriter:
    def __init__(self):
        self.pending = ""

    def write(self, value):
        self.pending += str(value)
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            emit("line", text=line.rstrip("\r"))
        return len(value)

    def flush(self):
        if self.pending:
            emit("line", text=self.pending.rstrip("\r"))
            self.pending = ""


def _run_command(command, state):
    writer = EventWriter()
    cancel_event = threading.Event()
    with state["lock"]:
        state["cancel"] = cancel_event
        state["approval"] = None

    try:
        with contextlib.redirect_stdout(writer):
            action = command.get("action")
            if action == "session_new":
                state["pending_session"] = True
                emit("session", session_id="", pending=True)
            elif action == "session_continue":
                import os
                if os.environ.get("EVERQUILL_PROVIDER", "meta") == "muse":
                    session_id = load_session(Path.cwd())
                else:
                    from everquill.session import load_provider_session
                    record = load_provider_session(Path.cwd(), "meta")
                    session_id = record["session_id"] if record else None
                state["pending_session"] = False
                emit("session", session_id=session_id or "")
            elif action == "request":
                mode = command.get("mode", "ask")
                prompt = command.get("prompt", "")

                def approve():
                    answer = queue.Queue(maxsize=1)
                    with state["lock"]:
                        state["approval"] = answer
                    emit("approval")
                    while not cancel_event.is_set():
                        try:
                            return answer.get(timeout=0.1)
                        except queue.Empty:
                            pass
                    return False

                def confirm_context(summary):
                    print(f"[everquill] Context preview: {summary['characters']} characters; "
                          f"{len(summary['files'])} candidate file(s).")
                    for name in summary["files"]:
                        print(f"  {ascii(name)}")
                    purpose = summary.get("purpose", "context")
                    if purpose == "tool_execution":
                        print("[everquill] Read-only recovery execution only; no result send approved.")
                    elif purpose == "tool_results":
                        print("[everquill] Only the reviewed tool results will be sent.")
                    else:
                        print("[everquill] Your request and project label will also be sent.")
                    answer = queue.Queue(maxsize=1)
                    with state["lock"]:
                        state["approval"] = answer
                    emit("approval", purpose=purpose)
                    while not cancel_event.is_set():
                        try:
                            return answer.get(timeout=0.1)
                        except queue.Empty:
                            pass
                    return False

                session_state = {
                    "id": command.get("session_id") or None,
                    "pending": state.get("pending_session", False),
                }
                status = run_agent_loop(
                    prompt,
                    proposal=mode in ("proposal", "iterate"),
                    session_id=command.get("session_id") or None,
                    iterate=mode == "iterate",
                    approve=approve,
                    confirm_context=confirm_context,
                    cancel_event=cancel_event,
                    session_state=session_state,
                )
                state["pending_session"] = session_state["pending"]
                if session_state["id"] and session_state["id"] != command.get("session_id"):
                    emit("session", session_id=session_state["id"])
                emit("result", status=status)
            else:
                raise ValueError("unknown UI action")
    except Exception:
        emit("error", message="Request failed. Check session and connection; no automatic retry.")
    finally:
        writer.flush()
        with state["lock"]:
            state["cancel"] = None
            state["approval"] = None
            state["active"] = False
        emit("done")


def main():
    state = {
        "lock": threading.Lock(), "cancel": None, "approval": None,
        "active": False, "pending_session": False,
    }
    active = None
    for raw in sys.stdin:
        try:
            command = json.loads(raw)
        except json.JSONDecodeError:
            emit("error", message="invalid UI protocol message")
            continue
        kind = command.get("type")
        if kind == "shutdown":
            break
        if kind == "cancel":
            with state["lock"]:
                if state["cancel"] is not None:
                    state["cancel"].set()
                approval = state["approval"]
            if approval is not None:
                try:
                    approval.put_nowait(False)
                except queue.Full:
                    pass
            continue
        if kind == "approval_response":
            with state["lock"]:
                approval = state["approval"]
            if approval is not None:
                try:
                    approval.put_nowait(command.get("approved") is True)
                except queue.Full:
                    pass
            continue
        if kind != "command":
            emit("error", message="unknown UI protocol message")
            continue
        with state["lock"]:
            already_active = state["active"]
            if not already_active:
                state["active"] = True
        if already_active:
            emit("line", text="Everquill is still working. Wait or cancel first.")
            continue
        active = threading.Thread(target=_run_command, args=(command, state))
        active.start()
    with state["lock"]:
        if state["cancel"] is not None:
            state["cancel"].set()
        approval = state["approval"]
    if approval is not None:
        try:
            approval.put_nowait(False)
        except queue.Full:
            pass
    if active is not None:
        active.join(timeout=3)


if __name__ == "__main__":
    main()
