"""Launch the Node.js TUI while reusing the Python Everquill agent backend."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


MIN_NODE_MAJOR = 22


def _node_command():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js 22 or newer is required for the Everquill TUI")
    try:
        result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not check Node.js runtime: {exc}") from exc
    match = re.fullmatch(r"v?(\d+)\.\d+\.\d+", result.stdout.strip())
    if result.returncode or not match or int(match.group(1)) < MIN_NODE_MAJOR:
        version = result.stdout.strip() or "unknown"
        raise RuntimeError(
            f"Node.js 22 or newer is required for the Everquill TUI (found {version})"
        )
    return node


def run_node_tui(project_root, session_id, mode, new_session=False):
    node = _node_command()
    app = Path(__file__).with_name("tui.mjs")
    if not app.is_file():
        raise RuntimeError(f"Node TUI entrypoint is missing: {app}")
    environment = os.environ.copy()
    environment["EVERQUILL_PYTHON"] = sys.executable
    environment.setdefault("PYTHONUTF8", "1")
    command = [node, str(app), "--mode", mode, "--session", session_id or ""]
    if new_session:
        command.append("--new-session")
    try:
        result = subprocess.run(
            command, cwd=project_root, env=environment, check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"could not start Node.js TUI: {exc}") from exc
    return result.returncode
