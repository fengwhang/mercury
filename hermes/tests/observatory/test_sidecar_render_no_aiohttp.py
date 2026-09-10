"""render_sidecar_unit importable without aiohttp (fresh-install law).

The unit file must install BEFORE the crypto stack (which provides
aiohttp) exists: ``provision.ensure_sidecar_unit`` renders pure string
templating and must never require the matrix extra. Regression: the render
lived in ``sidecar_main``, whose top-level ``raise SystemExit`` on missing
aiohttp made even the render unimportable. The canonical home is now
``config_gen`` (next to ``render_homeserver_unit``) with a back-compat
re-export from ``sidecar_main`` that never raises SystemExit at import.
"""
from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import pytest


def _render_kwargs() -> dict:
    return {
        "python_bin": "/opt/mercury/hermes/.venv/bin/python",
        "hermes_root": "/opt/mercury/hermes",
        "mercury_home": "/home/phoenix/.mercury",
        "log_dir": "/home/phoenix/.mercury/observatory/logs",
    }


class TestCanonicalHome:
    def test_config_gen_renders_without_aiohttp(self, monkeypatch):
        """config_gen never needs aiohttp: block the import, render anyway."""
        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "aiohttp" or name.startswith("aiohttp."):
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        for mod in ("aiohttp", "aiohttp.web"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
        from observatory import config_gen

        importlib.reload(config_gen)
        try:
            unit = config_gen.render_sidecar_unit(**_render_kwargs())
        finally:
            importlib.reload(config_gen)
        assert "ExecStart=/opt/mercury/hermes/.venv/bin/python -m observatory.sidecar_main" in unit
        assert "PYTHONPATH=/opt/mercury/hermes" in unit

    def test_sidecar_reexport_matches_config_gen(self):
        from observatory import config_gen
        from observatory import sidecar_main

        # Same contract (identity breaks across importlib.reload in-suite;
        # compare code + output, not object identity).
        assert sidecar_main.render_sidecar_unit.__name__ == "render_sidecar_unit"
        assert (sidecar_main.render_sidecar_unit.__code__.co_code
                == config_gen.render_sidecar_unit.__code__.co_code)
        kwargs = _render_kwargs()
        assert sidecar_main.render_sidecar_unit(**kwargs) == config_gen.render_sidecar_unit(**kwargs)


class TestSidecarImportWithoutAiohttp:
    @staticmethod
    def _purge_sidecar_modules() -> dict:
        saved: dict = {}
        for name in (
            "observatory.sidecar_main",
            "observatory.appservice",
            "observatory.matrix_client",
            "aiohttp",
            "aiohttp.web",
        ):
            if name in sys.modules:
                saved[name] = sys.modules.pop(name)
        return saved

    def test_import_render_never_raises_systemexit(self, monkeypatch):
        """'from observatory.sidecar_main import render_sidecar_unit' with a
        missing aiohttp: no SystemExit, no ImportError — and the render
        output matches config_gen exactly."""
        saved = self._purge_sidecar_modules()
        real_import = builtins.__import__

        def _blocked(name, *args, **kwargs):
            if name == "aiohttp" or name.startswith("aiohttp."):
                raise ImportError("No module named aiohttp (blocked for test)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _blocked)
        try:
            import observatory.sidecar_main as sm

            render = sm.render_sidecar_unit  # must not raise SystemExit
            unit = render(**_render_kwargs())
            from observatory import config_gen

            assert unit == config_gen.render_sidecar_unit(**_render_kwargs())
            # The daemon still fails fast — but lazily, at boot, not import.
            with pytest.raises(SystemExit):
                sm._require_aiohttp()
        finally:
            monkeypatch.undo()
            for name in ("observatory.sidecar_main", "observatory.appservice",
                         "observatory.matrix_client", "aiohttp", "aiohttp.web"):
                sys.modules.pop(name, None)
            sys.modules.update(saved)
            import observatory.sidecar_main  # noqa: F401 — restore normally

    def test_provision_imports_from_config_gen(self):
        """ensure_sidecar_unit must not go through sidecar_main (the old
        SystemExit path)."""
        src = (Path(__file__).resolve().parent.parent.parent
               / "observatory" / "provision.py").read_text(encoding="utf-8")
        assert "from observatory.config_gen import render_sidecar_unit" in src
        assert "from observatory.sidecar_main import render_sidecar_unit" not in src
