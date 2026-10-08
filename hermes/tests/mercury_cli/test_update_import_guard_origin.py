"""Prepared-source validation must not borrow first-party code from another tree."""

from mercury_cli import update_cmd


def _prepare(tmp_path, monkeypatch):
    root = tmp_path / "prepared"
    root.mkdir()
    monkeypatch.setattr(update_cmd, "_UPDATE_CRITICAL_MODULES", ("consumer",))
    # Use the running cached interpreter, without loading unrelated CLI state.
    monkeypatch.setattr(update_cmd, "_m", lambda: type("CLI", (), {"_is_windows": staticmethod(lambda: False)}))
    return root


def test_missing_root_cannot_borrow_ambient_tools(tmp_path, monkeypatch):
    root = _prepare(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign"
    (foreign / "tools").mkdir(parents=True)
    (foreign / "tools/__init__.py").write_text("")
    (foreign / "tools/owned_marker.py").write_text("VALUE = 1\n")
    (root / "consumer.py").write_text("from tools.owned_marker import VALUE\n")
    monkeypatch.setenv("PYTHONPATH", str(foreign))
    failures = update_cmd._critical_module_import_failures(root)
    assert failures["consumer"][0] == "ModuleNotFoundError"
    assert "'tools'" in failures["consumer"][1]


def test_prepared_package_wins_over_poison_pythonpath(tmp_path, monkeypatch):
    root = _prepare(tmp_path, monkeypatch)
    (root / "tools").mkdir()
    (root / "tools/__init__.py").write_text("")
    (root / "tools/owned_marker.py").write_text("VALUE = 42\n")
    (root / "consumer.py").write_text(
        "from tools.owned_marker import VALUE\nassert VALUE == 42\nimport json, pytest\n"
    )
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "tools.py").write_text("raise ImportError('poison executed')\n")
    monkeypatch.setenv("PYTHONPATH", str(poison))
    assert update_cmd._critical_module_import_failures(root, report_runtime_errors=True) == {}


def test_missing_child_is_not_missing_root(tmp_path, monkeypatch):
    root = _prepare(tmp_path, monkeypatch)
    (root / "tools").mkdir()
    (root / "tools/__init__.py").write_text("")
    (root / "consumer.py").write_text("import tools.absent_child\n")
    failures = update_cmd._critical_module_import_failures(root)
    assert failures["consumer"][0] == "ModuleNotFoundError"
    assert "'tools.absent_child'" in failures["consumer"][1]


def test_foreign_symlink_origin_is_rejected(tmp_path, monkeypatch):
    root = _prepare(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign_tools"
    foreign.mkdir()
    (foreign / "__init__.py").write_text("")
    (foreign / "owned_marker.py").write_text("VALUE = 42\n")
    (root / "tools").symlink_to(foreign, target_is_directory=True)
    (root / "consumer.py").write_text("from tools.owned_marker import VALUE\n")
    failures = update_cmd._critical_module_import_failures(root)
    assert failures["consumer"][0] == "ImportError"
    assert "outside prepared source root" in failures["consumer"][1]


def test_foreign_child_origin_is_rejected(tmp_path, monkeypatch):
    root = _prepare(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "owned_marker.py").write_text("VALUE = 42\n")
    (root / "tools").mkdir()
    (root / "tools/__init__.py").write_text(f"__path__ = [{str(foreign)!r}]\n")
    (root / "consumer.py").write_text("from tools.owned_marker import VALUE\n")
    failures = update_cmd._critical_module_import_failures(root)
    assert failures["consumer"][0] == "ImportError"
    assert "tools.owned_marker" in failures["consumer"][1]


def test_missing_third_party_dependency_remains_distinct(tmp_path, monkeypatch):
    root = _prepare(tmp_path, monkeypatch)
    (root / "consumer.py").write_text("import residual_uninstalled_dependency\n")
    assert update_cmd._critical_module_import_failures(root) == {}
    failures = update_cmd._critical_module_import_failures(root, report_runtime_errors=True)
    assert failures["consumer"][0] == "ModuleNotFoundError"
    assert "residual_uninstalled_dependency" in failures["consumer"][1]
