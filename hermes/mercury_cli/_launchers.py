"""Inspect a managed launcher's interpreter without running it."""
from pathlib import Path
import shlex


def _launcher_python(target: Path) -> Path | None:
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        if line.startswith("exec "):
            lexer = shlex.shlex(line.removeprefix("exec "), posix=True)
            lexer.whitespace_split = True
            try:
                token = lexer.get_token()
            except ValueError:
                return None
            return Path(token) if token and Path(token).is_absolute() else None
        if target.suffix == ".cmd" and line.startswith('"'):
            end = line.find('"', 1)
            if end > 1:
                return Path(line[1:end])
    return None
