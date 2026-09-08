"""M3c sim harness tests: timeline determinism + the event-shape contract.

Two laws, both load-bearing for the M3c E2E gate and M4 tests:

1. **Determinism** — two independently constructed ``ScriptedTimeline`` s are
   equal, timestamps strictly increase, replays are byte-identical, and the
   story signature is pinned (order of event classes + kinds + times).
2. **Contract-import** — every sim constructor returns an instance of the
   REAL dataclass from its producing module (imported here independently of
   sim.py), so a field added/renamed upstream breaks the sim loudly instead
   of drifting into a duplicate shape.

Plus the offline provisioning gate: ``--offline`` / ``observatory.offline``
config makes the binary step trust ``tuwunel.version`` and never touch the
network (fail-hard when there is nothing installed to trust).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "hermes"))

from observatory import sim  # noqa: E402
from observatory import provision, tuwunel  # noqa: E402
from observatory.config_gen import ObservatoryPaths  # noqa: E402
from observatory.discovery import NodeEvent as DiscoveryNodeEvent  # noqa: E402
from observatory.omp_feed import (  # noqa: E402
    MessageEvent,
    NodeEvent as OmpNodeEvent,
    ThoughtEvent,
    ToolEvent,
)


# --- determinism -----------------------------------------------------------------


class TestTimelineDeterminism:
    def test_two_constructions_are_identical(self):
        assert ScriptedTimeline_equal(sim.ScriptedTimeline(), sim.ScriptedTimeline())

    def test_timestamps_strictly_increase(self):
        ts = [te.t for te in sim.ScriptedTimeline()]
        assert ts == sorted(ts)
        assert len(set(ts)) == len(ts)

    def test_replay_is_stable(self):
        tl = sim.ScriptedTimeline()
        first: list = []
        second: list = []
        sim.drive(tl, first.append)
        sim.drive(tl, second.append)
        assert first == second
        assert first == [te.event for te in tl]

    def test_story_signature_is_pinned(self):
        """(t, class, kind) sequence — the canned scenario, frozen."""
        sig = [
            (te.t, type(te.event).__name__, getattr(te.event, "kind", ""))
            for te in sim.ScriptedTimeline()
        ]
        assert sig == [
            (0.0, "NodeEvent", "add"),      # orchestrator
            (1.0, "NodeEvent", "add"),      # child A
            (1.5, "NodeEvent", "add"),      # child B
            (3.0, "NodeEvent", "add"),      # grandchild
            (4.0, "ThoughtEvent", ""),
            (4.5, "ToolEvent", ""),
            (6.0, "ThoughtEvent", ""),
            (6.5, "ToolEvent", ""),
            (8.0, "MessageEvent", ""),
            (10.0, "ToolEvent", ""),
            (12.0, "MessageEvent", ""),
            (13.0, "NodeEvent", "death"),  # grandchild
            (14.0, "NodeEvent", "death"),  # child A
            (15.0, "NodeEvent", "death"),  # child B
            (22.0, "NodeEvent", "death"),  # orchestrator, last
        ]

    def test_story_text_lists_every_beat(self):
        lines = sim.ScriptedTimeline().story().splitlines()
        assert len(lines) == len(sim.ScriptedTimeline())
        assert lines[0].endswith(f"kind=add {sim.ORCH_NAME!r} parent={sim.GATEWAY_SESSION}")


def ScriptedTimeline_equal(a: "sim.ScriptedTimeline", b: "sim.ScriptedTimeline") -> bool:
    return a.events == b.events


# --- scenario invariants -----------------------------------------------------------


class TestScenarioStory:
    def test_first_event_is_orchestrator_add(self):
        first = sim.ScriptedTimeline().events[0].event
        assert isinstance(first, DiscoveryNodeEvent)
        assert (first.kind, first.name, first.parent_session) == (
            "add", sim.ORCH_NAME, sim.GATEWAY_SESSION,
        )

    def test_last_event_is_orchestrator_death(self):
        last = sim.ScriptedTimeline().events[-1].event
        assert isinstance(last, DiscoveryNodeEvent)
        assert (last.kind, last.child_session_id) == ("death", sim.ORCH_SESSION)

    def test_deaths_are_ordered_grandchild_children_root(self):
        tl = sim.ScriptedTimeline()
        gc_death = next(
            te.t for te in tl
            if isinstance(te.event, OmpNodeEvent) and te.event.kind == "death"
        )
        child_deaths = [
            te.t for te in tl
            if isinstance(te.event, DiscoveryNodeEvent) and te.event.kind == "death"
            and te.event.parent_session == sim.ORCH_SESSION
        ]
        orch_death = next(
            te.t for te in tl
            if isinstance(te.event, DiscoveryNodeEvent) and te.event.kind == "death"
            and te.event.parent_session == sim.GATEWAY_SESSION
        )
        assert gc_death < child_deaths[0] < child_deaths[1] < orch_death

    def test_every_death_had_a_prior_add(self):
        tl = sim.ScriptedTimeline()
        seen_disc: set[tuple[str, int]] = set()
        seen_gc: set[str] = set()
        for te in tl:
            ev = te.event
            if isinstance(ev, DiscoveryNodeEvent):
                key = (ev.delegation_id, ev.task_index)
                if ev.kind == "add":
                    seen_disc.add(key)
                else:
                    assert key in seen_disc, f"death without add: {key}"
            elif isinstance(ev, OmpNodeEvent):
                if ev.kind == "add":
                    seen_gc.add(ev.subagent_id)
                else:
                    assert ev.subagent_id in seen_gc

    def test_orchestrator_quiet_gap_while_live(self):
        """Between child B's death and the orchestrator's death the renderer
        must show the orchestrator LIVE with zero incoming events."""
        tl = sim.ScriptedTimeline()
        child_b_death_t = 15.0
        orch_death_t = next(
            te.t for te in tl
            if isinstance(te.event, DiscoveryNodeEvent) and te.event.kind == "death"
            and te.event.parent_session == sim.GATEWAY_SESSION
        )
        quiet = [te for te in tl if child_b_death_t < te.t < orch_death_t]
        assert quiet == []
        assert orch_death_t - child_b_death_t >= 5.0


# --- event-shape contract (import the REAL classes; sim must not duplicate) --------


class TestEventShapeContract:
    def test_constructors_return_the_real_dataclasses(self):
        assert isinstance(
            sim.node_add(
                "n", "g", parent_session="p", delegation_id="deleg_x",
                task_index=0, seq=1, child_session_id="s",
            ),
            DiscoveryNodeEvent,
        )
        assert isinstance(
            sim.node_death(
                parent_session="p", delegation_id="deleg_x", task_index=0,
                seq=2, child_session_id="s",
            ),
            DiscoveryNodeEvent,
        )
        assert isinstance(sim.gc_add(seq=1), OmpNodeEvent)
        assert isinstance(sim.gc_death(seq=2), OmpNodeEvent)
        assert isinstance(sim.tool_call("bash", "echo hi", seq=3), ToolEvent)
        assert isinstance(sim.thought("hm", seq=4), ThoughtEvent)
        assert isinstance(sim.message("assistant", "done", seq=5), MessageEvent)

    def test_timeline_events_are_all_real_classes(self):
        allowed = (DiscoveryNodeEvent, OmpNodeEvent, ToolEvent, ThoughtEvent, MessageEvent)
        for te in sim.ScriptedTimeline():
            assert isinstance(te.event, allowed)

    def test_overrides_win_and_unknown_fields_fail_loud(self):
        ev = sim.tool_call("bash", "x", seq=1, subagent_id="other-gc")
        assert (ev.subagent_id, ev.tool) == ("other-gc", "bash")
        with pytest.raises(TypeError, match="no field"):
            sim.tool_call("bash", "x", seq=1, bogus="nope")

    def test_seq_streams_mirror_the_real_emitters(self):
        """Discovery seq: engine-wide 1..N; omp_feed seq: feed-wide 1..M."""
        disc = [
            te.event.seq for te in sim.ScriptedTimeline()
            if isinstance(te.event, DiscoveryNodeEvent)
        ]
        omp = [
            te.event.seq for te in sim.ScriptedTimeline()
            if isinstance(te.event, (OmpNodeEvent, ToolEvent, ThoughtEvent, MessageEvent))
        ]
        assert disc == list(range(1, len(disc) + 1))
        assert omp == list(range(1, len(omp) + 1))

    def test_timeline_covers_every_event_type(self):
        kinds = {type(te.event) for te in sim.ScriptedTimeline()}
        assert kinds == {
            DiscoveryNodeEvent, OmpNodeEvent, ToolEvent, ThoughtEvent, MessageEvent,
        }


# --- driver -------------------------------------------------------------------------


class TestDriver:
    def test_feeds_events_in_order(self):
        tl = sim.ScriptedTimeline()
        fed: list = []
        out = sim.drive(tl, fed.append)
        assert out == fed == [te.event for te in tl]

    def test_realtime_paces_on_schedule_but_not_without_it(self):
        tl = sim.ScriptedTimeline()
        sleeps: list[float] = []

        sim.drive(tl, lambda ev: None, realtime=True, sleeper=sleeps.append)
        assert sleeps  # paced…
        assert sum(sleeps) == pytest.approx(tl.duration())

        sleeps.clear()
        sim.drive(tl, lambda ev: None, sleeper=sleeps.append)
        assert sleeps == []  # …and only when asked

    def test_ingest_receives_events_only(self):
        seen: list = []
        sim.drive(sim.ScriptedTimeline(), lambda ev: seen.append(ev))
        assert all(not isinstance(x, float) for x in seen)


# --- offline provisioning -------------------------------------------------------------


def _boom(url: str) -> bytes:
    raise AssertionError(f"network touched in offline mode: {url}")


@pytest.fixture
def seeded_home(tmp_path: Path) -> Path:
    """A pre-fetched home: binary + version file + existing owner creds
    (registration boots a server — offline tests must skip that step)."""
    paths = ObservatoryPaths(tmp_path)
    paths.bin_dir.mkdir(parents=True)
    paths.binary.write_bytes(b"#!/bin/sh\n# fake pre-fetched tuwunel\n")
    paths.version_file.write_text("1.9.0\n", encoding="utf-8")
    paths.owner_credentials.write_text("{}\n", encoding="utf-8")
    return tmp_path


class TestProvisionOffline:
    def test_offline_skips_the_fetch_entirely(self, seeded_home: Path):
        summary = provision.provision(
            seeded_home, systemd=False, offline=True, fetch=_boom
        )
        assert summary["tuwunel"]["action"] == "current"
        assert summary["tuwunel"]["version"] == "1.9.0"
        assert summary["tuwunel"]["offline"] is True
        assert summary["config"] == "created"
        assert summary["appservice"] == "created"
        assert summary["owner"] == "exists"
        assert summary["unit"] == "skipped (--no-systemd)"

    def test_offline_is_idempotent(self, seeded_home: Path):
        first = provision.provision(seeded_home, systemd=False, offline=True, fetch=_boom)
        second = provision.provision(seeded_home, systemd=False, offline=True, fetch=_boom)
        assert first["tuwunel"]["action"] == second["tuwunel"]["action"] == "current"
        assert second["config"] == "kept"
        assert second["appservice"] == "kept"

    def test_offline_without_installed_binary_fails_hard(self, tmp_path: Path):
        with pytest.raises(tuwunel.TuwunelError, match="offline"):
            provision.provision(tmp_path, systemd=False, offline=True, fetch=_boom)

    def test_offline_below_min_version_fails_hard(self, seeded_home: Path):
        paths = ObservatoryPaths(seeded_home)
        paths.version_file.write_text("1.8.0\n", encoding="utf-8")
        with pytest.raises(tuwunel.TuwunelError, match="1.8.1"):
            provision.provision(seeded_home, systemd=False, offline=True, fetch=_boom)

    def test_offline_resolves_from_config_not_env(self, seeded_home: Path):
        """`observatory.offline: true` in config.yaml gates it — no env var."""
        import mercury_cli.config as mc

        original = mc.load_config_readonly
        mc.load_config_readonly = lambda: {"observatory": {"offline": True}}
        try:
            summary = provision.provision(seeded_home, systemd=False, fetch=_boom)
        finally:
            mc.load_config_readonly = original
        assert summary["tuwunel"]["offline"] is True

    def test_cli_offline_flag(self, seeded_home: Path):
        assert provision.main(
            ["--mercury-home", str(seeded_home), "--no-systemd", "--offline"]
        ) == 0
