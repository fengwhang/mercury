"""`!verb` -> `/verb` passthrough: every command on both engines.

The hermes side resolves dynamically (is_gateway_known_command, plugins
included); the omp side is a static mirror of the TS registry. This file
pins the two together: the drift test fails loudly when TS adds a verb
the mirror lacks, and behavior tests cover the rewrite gate.
"""

from __future__ import annotations

import re
from pathlib import Path


def _ts_omp_verbs() -> set[str]:
    root = Path(__file__).parents[5]
    names: set[str] = set()
    for f in (root / "omp/packages/coding-agent/src/slash-commands").glob(
        "builtin-*.ts"
    ):
        names.update(re.findall(r'name: "([a-z0-9-]+)"', f.read_text()))
    return names


def test_omp_mirror_covers_ts_registry() -> None:
    from plugins.platforms.irc.adapter import OMP_BANG_VERBS

    ts_verbs = _ts_omp_verbs()
    assert ts_verbs, "TS registry unreadable — mirror cannot be verified"
    assert ts_verbs <= set(OMP_BANG_VERBS), (
        "OMP added slash verbs missing from OMP_BANG_VERBS: "
        + ", ".join(sorted(ts_verbs - set(OMP_BANG_VERBS)))
    )


def test_bang_passthrough_both_engines() -> None:
    from plugins.platforms.irc.adapter import bang_to_slash

    # hermes-side (dynamic registry)
    assert bang_to_slash("!model opus") == "/model opus"
    assert bang_to_slash("!status") == "/status"
    assert bang_to_slash("!HELP") == "/help"
    # omp-side (static mirror)
    assert bang_to_slash("!compact now") == "/compact now"
    assert bang_to_slash("!session list") == "/session list"
    # original core verbs still rewrite
    assert bang_to_slash("!spawn agent") == "/spawn agent"
    assert bang_to_slash("!exit") == "/exit"
    assert bang_to_slash("!approve") == "/approve"
    # unknown verbs stay chat; escapes hold
    assert bang_to_slash("!wow amazing") == "!wow amazing"
    assert bang_to_slash("hello!") == "hello!"
    assert bang_to_slash("!!model x") == "!!model x"
    assert bang_to_slash("!") == "!"


def test_bang_falls_back_to_core_verbs(monkeypatch) -> None:
    from plugins.platforms.irc import adapter as adapter_mod

    def _boom(verb):
        raise ImportError("no registry")

    monkeypatch.setattr(adapter_mod, "_is_hermes_known_verb", _boom)
    assert adapter_mod.bang_to_slash("!spawn x") == "/spawn x"
    assert adapter_mod.bang_to_slash("!whoami") == "!whoami"
