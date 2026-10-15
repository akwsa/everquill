from pathlib import Path

from .tools import glob_files, read_file


IMPORTANT_FILES = [
    "README.md",
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "AGENTS.md",
    "CLAUDE.md",
]


def build_project_context(root=None, selected_files=None):
    root = Path(root or ".").resolve()

    sections = []

    files = glob_files("**/*", root=str(root), limit=150)
    if selected_files is not None:
        selected_files.extend(files)

    sections.append(
        "PROJECT FILES:\n"
        + "\n".join(files)
    )

    for name in IMPORTANT_FILES:
        path = root / name

        if path.exists() and path.is_file():
            try:
                content = read_file(name, root=str(root), max_chars=4000)
            except Exception:
                continue

            sections.append(
                f"\n--- {name} ---\n{content}"
            )

    return "\n".join(sections)
