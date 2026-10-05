"""Compatibility gates used by Mercury's plugin dependency installer.

Legacy ``requires_hermes`` describes the pinned Hermes API, not Mercury's
independently numbered product release. New plugins can use ``requires_mercury``.
"""
from collections.abc import Mapping
import re

SUPPORTED_MANIFEST_VERSION = 2
HERMES_API_VERSION = "0.21.0"  # Hermes pin recorded in PINS.txt.


def _version_tuple(value: str) -> tuple[int, ...] | None:
    parts = re.split(r"[-+]", str(value).strip().lstrip("v"), 1)[0].split(".")
    parts += ["0"] * max(0, 3 - len(parts))
    matches = [re.match(r"^\d+", part.strip()) for part in parts[:3]]
    return tuple(int(match.group()) for match in matches) if all(matches) else None


def version_satisfies(spec: str, current: str) -> bool:
    cur = _version_tuple(current)
    if cur is None:
        return True
    ops = {">=": cur.__ge__, "<=": cur.__le__, "==": cur.__eq__, "!=": cur.__ne__,
           ">": cur.__gt__, "<": cur.__lt__}
    for clause in filter(None, (part.strip() for part in spec.split(","))):
        match = re.match(r"^(>=|<=|==|!=|>|<)\s*(.+)$", clause)
        op, value = match.groups() if match else (">=", clause)
        target = _version_tuple(value)
        if target is not None and not ops[op](target):
            return False
    return True


def requires_hermes_error(manifest) -> str | None:
    from mercury_cli import __version__

    for key, label, current in (("requires_mercury", "Mercury", __version__),
                                ("requires_hermes", "Hermes API", HERMES_API_VERSION)):
        spec = manifest.get(key, "") if isinstance(manifest, Mapping) else getattr(manifest, key, "")
        if spec and not version_satisfies(str(spec), current):
            return f"requires {label} {spec}, running {current}"
    return None
