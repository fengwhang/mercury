"""VM-report slice 5 (defect vii): observatory.mirror_cli gate, default off.

CLI/manual TUI sessions were mirrored into Matrix rooms with no opt-out.
Now: ``off`` (default — sessions never get rooms), ``observe`` (presence
rooms only, no transcript content), ``full`` (rooms + live transcripts).
Unknown values fail closed to ``off``.

Real files throughout (tmp MERCURY_HOME config.yaml + real omp session
JSONLs); no homeserver.
"""
from __future__ import annotations

from pathlib import Path

from observatory import provision as provision_mod
from observatory.manual_runs import ManualRunsWatcher
from observatory.renderer import IntentExecutor, Renderer
from observatory.state import ObservatoryState
from tests.observatory.test_manual_runs import (
    assistant_msg,
    seed_state,
    user_msg,
    write_session,
)

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"


def _write_config(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")


def _watcher(tmp_path: Path, agent: Path, *, mode: str, state=None):
    st = state if state is not None else seed_state(tmp_path)
    renderer = Renderer(st, gateway_node_id=GW, server_name=SERVER,
                        owner_mxid=OWNER, executor=None)
    return ManualRunsWatcher(renderer, agent_dir=agent, mode=mode), st


class TestMirrorCliMode:
    def test_missing_config_is_off(self, tmp_path):
        assert provision_mod.mirror_cli_mode(tmp_path) == "off"

    def test_no_observatory_key_is_off(self, tmp_path):
        _write_config(tmp_path, "model:\n  default: x/y\n")
        assert provision_mod.mirror_cli_mode(tmp_path) == "off"

    def test_observe_and_full_pass_through(self, tmp_path):
        _write_config(tmp_path, "observatory:\n  mirror_cli: observe\n")
        assert provision_mod.mirror_cli_mode(tmp_path) == "observe"
        _write_config(tmp_path, "observatory:\n  mirror_cli: FULL\n")
        assert provision_mod.mirror_cli_mode(tmp_path) == "full"

    def test_bogus_fails_closed_to_off(self, tmp_path):
        _write_config(tmp_path, "observatory:\n  mirror_cli: yes-please\n")
        assert provision_mod.mirror_cli_mode(tmp_path) == "off"
        _write_config(tmp_path, "observatory: [not, a, dict]\n")
        assert provision_mod.mirror_cli_mode(tmp_path) == "off"


class TestWatcherGate:
    def _session(self, tmp_path: Path) -> Path:
        agent = tmp_path / "omp"
        write_session(
            agent, "sess_abc123",
            lines=[user_msg("hello"), assistant_msg({"type": "text", "text": "hi"})],
        )
        return agent

    def test_default_off_creates_nothing(self, tmp_path):
        agent = self._session(tmp_path)
        watcher, state = _watcher(tmp_path, agent, mode="off")
        result = watcher.poll()
        assert result.new_nodes == [] and result.events == {} and result.reaped == []
        assert [r for r in state.get_live()
                if (r.get("extra") or {}).get("kind") == "manual-run"] == []

    def test_unknown_mode_fails_closed(self, tmp_path):
        agent = self._session(tmp_path)
        watcher, state = _watcher(tmp_path, agent, mode="everything")
        assert watcher.mode == "off"
        assert watcher.poll().new_nodes == []

    def test_observe_creates_rooms_without_transcripts(self, tmp_path):
        agent = self._session(tmp_path)
        watcher, _ = _watcher(tmp_path, agent, mode="observe")
        result = watcher.poll()
        assert len(result.new_nodes) == 1
        assert result.new_nodes[0]["engine"] == "omp"
        assert result.events == {}

    def test_full_merges_transcripts(self, tmp_path):
        agent = self._session(tmp_path)
        watcher, _ = _watcher(tmp_path, agent, mode="full")
        result = watcher.poll()
        assert len(result.new_nodes) == 1
        node_id = result.new_nodes[0]["node_id"]
        assert result.events.get(node_id), "full must forward transcript events"


def test_setup_card_documents_gate(monkeypatch, tmp_path, capsys):
    import mercury_cli.setup as setup_mod

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    assert "never get rooms" in setup_mod._mirror_cli_card_line()
    _write_config(tmp_path, "observatory:\n  mirror_cli: full\n")
    assert "full" in setup_mod._mirror_cli_card_line()
