"""Plugin selection must operate on the engine view and preserve shared model authority."""
import pytest
import yaml

from pm.plugins_state import read_home_selection


@pytest.fixture
def home(tmp_path):
    config = {
        "hermes": {"plugins": {"enabled": ["example"], "disabled": []},
                   "memory": {"provider": "example"}},
        "models": {"default": "nous/example"},
        "profile_models": {"work": {"default": "nous/other"}},
        "omp": {"tools": {"approvalMode": "write"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    return tmp_path, config


def test_reader_accepts_nested_and_legacy_engine_view(home):
    root, config = home
    assert read_home_selection(root) == config["hermes"]
    (root / "config.yaml").write_text(yaml.safe_dump(config["hermes"]))
    assert read_home_selection(root) == config["hermes"]


def test_reader_refuses_corrupt_configuration(home):
    root, _ = home
    for raw in ("hermes: []", "hermes: {plugins: []}", "hermes: [unclosed"):
        (root / "config.yaml").write_text(raw)
        with pytest.raises(ValueError):
            read_home_selection(root)


def test_plugin_selection_preserves_shared_and_other_engine_settings(home, monkeypatch):
    from pm import publication

    root, original = home
    monkeypatch.setattr(publication, "dependency_home_root", lambda: root)
    monkeypatch.setattr(publication, "selection_snapshot", lambda: {})
    monkeypatch.setattr(publication, "candidate_members", lambda *args, **kwargs: [])
    selection = publication.PluginSelection({"home": str(root), "enabled": ["new"], "disabled": []})
    proposed = yaml.safe_load(selection.proposed)
    assert proposed["hermes"]["plugins"]["enabled"] == ["new"]
    for section in ("models", "profile_models", "omp"):
        assert proposed[section] == original[section]
    assert "plugins" not in proposed
    assert yaml.safe_load((root / "config.yaml").read_text()) == original


def test_eviction_modifies_only_hermes_plugin_and_memory_settings(home, monkeypatch):
    from pm import publication, plugin_eviction

    root, original = home
    monkeypatch.setattr(publication, "selection_snapshot", lambda: {})
    plugin = root / "plugins" / "example"
    eviction = plugin_eviction.PluginEviction([(plugin.parent, "example", plugin)], {plugin: "incompatible"})
    proposed = yaml.safe_load(eviction.edits[0][2])
    assert proposed["hermes"]["plugins"]["disabled"] == ["example"]
    assert proposed["hermes"]["memory"]["provider"] == ""
    for section in ("models", "profile_models", "omp"):
        assert proposed[section] == original[section]


def test_plugin_manifest_reader_uses_available_yaml_runtime(tmp_path):
    from pm.plugin_declarations import read_native_manifest, read_python_declaration

    manifest = {"name": "example", "python_runtime": "external"}
    path = tmp_path / "plugin.yaml"
    path.write_text(yaml.safe_dump(manifest))
    assert read_native_manifest(path) == manifest
    assert read_python_declaration(tmp_path).external


def test_dependency_union_reads_real_mercury_profile_layout(tmp_path, monkeypatch):
    from pm.plugins_state import enabled_plugins_ordered
    from mercury_constants import mark_named_profile_deleted

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    (tmp_path / "config.yaml").write_text('hermes: {plugins: {enabled: [main-plugin]}}\nmodels: {default: "nous/main"}\n')
    profile = tmp_path / "hermes" / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text('hermes: {plugins: {enabled: [profile-plugin]}}\n')
    deleted = profile.parent / "deleted"
    deleted.mkdir()
    (deleted / "config.yaml").write_text('plugins: {enabled: [deleted-plugin]}\n')
    mark_named_profile_deleted(deleted)
    (profile.parent / "stray").mkdir()
    selected = enabled_plugins_ordered()
    assert selected[tmp_path / "hermes" / "plugins"] == ["main-plugin"]
    assert selected[profile / "plugins"] == ["profile-plugin"]
    assert deleted / "plugins" not in selected
    assert profile.parent / "stray" / "plugins" not in selected


def test_default_engine_selection_publishes_to_central_mercury_config(home, monkeypatch):
    from pm import publication

    root, original = home
    engine = root / "hermes"
    engine.mkdir()
    monkeypatch.setenv("MERCURY_HOME", str(root))
    monkeypatch.setenv("HERMES_HOME", str(engine))
    monkeypatch.setattr(publication, "selection_snapshot", lambda: {})
    monkeypatch.setattr(publication, "candidate_members", lambda *args, **kwargs: [])
    selection = publication.PluginSelection({"home": str(engine), "enabled": ["new"], "disabled": []})
    assert selection.path == root / "config.yaml"
    proposed = yaml.safe_load(selection.proposed)
    assert proposed["hermes"]["plugins"]["enabled"] == ["new"]
    for section in ("models", "profile_models", "omp"):
        assert proposed[section] == original[section]
    assert not (engine / "config.yaml").exists()
