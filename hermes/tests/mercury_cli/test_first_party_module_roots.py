"""Mercury's own modules share guard and diagnostic identity."""
import pytest

from mercury_cli import update_cmd
from mercury_constants import FIRST_PARTY_MODULE_ROOTS, is_first_party_module, partial_update_hint


@pytest.mark.parametrize("module", ["mercury_constants", "mercury_cli.config"])
def test_missing_mercury_module_blocks_prepared_source(module, tmp_path, monkeypatch):
    (tmp_path / "consumer.py").write_text(f"import {module}\n")
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))
    monkeypatch.setattr(update_cmd, "_m", lambda: type("CLI", (), {"_is_windows": staticmethod(lambda: False)}))
    failures = update_cmd._critical_module_import_failures(tmp_path)
    assert failures["consumer"][0] == "ModuleNotFoundError"
    assert module.split(".")[0] in failures["consumer"][1]
    assert module.split(".")[0] in FIRST_PARTY_MODULE_ROOTS
    assert partial_update_hint(ImportError("missing exported name", name=module))


@pytest.mark.parametrize("module", ["agent.context_compressor", "tools.todo_tool", "cli", "hermes_constants", "hermes_cli.config"])
def test_legitimate_legacy_identity_is_preserved(module):
    assert is_first_party_module(module)
    assert partial_update_hint(ImportError("missing exported name", name=module))


@pytest.mark.parametrize("module", ["agents", "agentops", "toolsets_x", "hermesx", "pytest", "mercury_cli_extra"])
def test_third_party_lookalikes_are_not_owned(module):
    assert not is_first_party_module(module)
    assert partial_update_hint(ImportError("third party", name=module)) == []
