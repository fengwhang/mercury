"""Event-simulation harness for the Matrix Observatory (M3c support slice).

Deterministic, network-free, binary-free replays of the exact typed events
the real feeds produce — so the M3c renderer E2E gate and the M4 tests can
drive a full 3-deep fan-out story without hermes state.db, omp RPC
processes, or a homeserver.

Law: this module NEVER redefines an event shape. Every constructor returns
an instance of the REAL dataclass imported from its producing module —

* ``observatory.discovery.NodeEvent``   — delegation-keyed child add/death
* ``observatory.omp_feed.NodeEvent``    — grandchild (omp in-process subagent) add/death
* ``observatory.omp_feed.ToolEvent``    — current tool invocation of a subagent
* ``observatory.omp_feed.ThoughtEvent`` — one accumulated thinking block
* ``observatory.omp_feed.MessageEvent`` — a completed assistant/user message

``tests/observatory/test_sim.py`` enforces that contract with isinstance
checks against classes imported independently from the real modules; if a
field is added upstream, the constructors here break loudly, not silently.

Scenario (one ``ScriptedTimeline()``, fixed ids, fixed sim-times):

    gateway session spawns orchestrator
      orchestrator adds (discovery; a gateway-origin delegation — it
      renders as a subspace nested under the gateway agent's own
      subspace, gw-space parity)
      child A "provision-audit" adds (discovery, task 0)
      child B "renderer-core" adds (discovery, task 1)
      B spawns grandchild "gc-render-checks" (omp_feed.NodeEvent add)
      tool calls + thinking + messages from the grandchild (omp_feed)
      grandchild dies (completed)
      child A dies (completed, summary)
      child B dies (completed, summary)
      ... orchestrator stays live (a scheduled quiet gap) ...
      orchestrator dies (completed, summary)

Seq stamping mirrors the real emitters: discovery events carry an
engine-wide monotonic ``seq`` (1..N in emission order), omp_feed events a
feed-wide monotonic ``seq`` (1..M) — exactly what ``DiscoveryEngine._emit``
and ``OmpFeed`` stamp on the wire.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Sequence, TypeVar

from observatory.discovery import NodeEvent as _DiscoveryNodeEvent
from observatory.omp_feed import (
    MessageEvent as _MessageEvent,
)
from observatory.omp_feed import (
    NodeEvent as _OmpNodeEvent,
)
from observatory.omp_feed import (
    ThoughtEvent as _ThoughtEvent,
)
from observatory.omp_feed import (
    ToolEvent as _ToolEvent,
)

#: Anything a feed can hand the renderer. Kept loose on purpose: the union
#: lives in the producing modules; the renderer owns its own dispatch.
SimEvent = object

#: What the renderer's ingest looks like from the sim's side.
Ingest = Callable[[SimEvent], object]

E = TypeVar("E")


# ---------------------------------------------------------------------------
# Fixed cast (deterministic ids — the whole point of the canned scenario)
# ---------------------------------------------------------------------------

GATEWAY_SESSION = "sess-gateway-0000"

ORCH_SESSION = "sess-orch-1000"
ORCH_DELEGATION = "deleg_0f1e2d3c4b"
ORCH_NAME = "observatory-gate"
ORCH_GOAL = "Land the M3c renderer E2E gate"

CHILD_DELEGATION = "deleg_5a6b7c8d9e"

CHILD_A_SESSION = "sess-child-2001"
CHILD_A_NAME = "provision-audit"
CHILD_A_GOAL = "Verify offline provisioning against the E2E testhome"

CHILD_B_SESSION = "sess-child-2002"
CHILD_B_NAME = "renderer-core"
CHILD_B_GOAL = "Stream tool calls and thinking into agent rooms"

GC_ID = "gc-3f8a1c"
GC_TOOL_CALL = "call_task_12"
GC_TASK = "Verify renderer room plan against a 3-deep fan-out"
GC_SESSION_FILE = (
    "/tmp/observatory-testhome/omp/sessions/"
    "--tmp-observatory--/20260908_101500_3f8a1c.jsonl"
)


# ---------------------------------------------------------------------------
# Constructors — thin, defaults first, explicit fields required where the
# scenario needs them readable at the call site. Each returns the REAL
# dataclass; ``overrides`` reapplied last so tweaks always win.
# ---------------------------------------------------------------------------


def _merge(event: E, **overrides: object) -> E:
    from dataclasses import replace

    unknown = set(overrides) - {
        f.name for f in event.__dataclass_fields__.values()  # type: ignore[attr-defined]
    }
    if unknown:
        raise TypeError(
            f"{type(event).__name__} has no field(s): {', '.join(sorted(unknown))}"
        )
    return replace(event, **overrides)  # type: ignore[return-value]


def node_add(
    name: str,
    goal: str,
    *,
    parent_session: str,
    delegation_id: str,
    task_index: int,
    seq: int,
    child_session_id: str,
    child_subagent_id: str | None = None,
    source: str = "hook",
    **overrides: object,
) -> _DiscoveryNodeEvent:
    """A discovery-side child spawn (delegation-keyed)."""
    return _merge(
        _DiscoveryNodeEvent(
            kind="add",
            delegation_id=delegation_id,
            task_index=task_index,
            parent_session=parent_session,
            name=name,
            goal=goal,
            status="running",
            source=source,
            seq=seq,
            child_session_id=child_session_id,
            child_subagent_id=child_subagent_id,
        ),
        **overrides,
    )


def node_death(
    *,
    parent_session: str,
    delegation_id: str,
    task_index: int,
    seq: int,
    child_session_id: str,
    status: str = "completed",
    summary: str | None = None,
    child_subagent_id: str | None = None,
    **overrides: object,
) -> _DiscoveryNodeEvent:
    """A discovery-side child death (terminal status + final summary)."""
    return _merge(
        _DiscoveryNodeEvent(
            kind="death",
            delegation_id=delegation_id,
            task_index=task_index,
            parent_session=parent_session,
            name="",
            goal="",
            status=status,
            source="hook",
            seq=seq,
            child_session_id=child_session_id,
            child_subagent_id=child_subagent_id,
            summary=summary,
        ),
        **overrides,
    )


def gc_add(
    *,
    seq: int,
    subagent_id: str = GC_ID,
    parent_tool_call_id: str | None = GC_TOOL_CALL,
    task: str | None = GC_TASK,
    session_file: str | None = GC_SESSION_FILE,
    **overrides: object,
) -> _OmpNodeEvent:
    """A grandchild (omp in-process subagent) spawn."""
    return _merge(
        _OmpNodeEvent(
            kind="add",
            subagent_id=subagent_id,
            parent_tool_call_id=parent_tool_call_id,
            status="running",
            seq=seq,
            index=0,
            agent="task",
            task=task,
            session_file=session_file,
        ),
        **overrides,
    )


def gc_death(
    *,
    seq: int,
    subagent_id: str = GC_ID,
    parent_tool_call_id: str | None = GC_TOOL_CALL,
    status: str = "completed",
    **overrides: object,
) -> _OmpNodeEvent:
    """A grandchild death (completed | failed | aborted)."""
    return _merge(
        _OmpNodeEvent(
            kind="death",
            subagent_id=subagent_id,
            parent_tool_call_id=parent_tool_call_id,
            status=status,
            seq=seq,
        ),
        **overrides,
    )


def tool_call(
    tool: str,
    args: str | None,
    *,
    seq: int,
    subagent_id: str = GC_ID,
    **overrides: object,
) -> _ToolEvent:
    """The subagent's current tool invocation (progress frames)."""
    return _merge(
        _ToolEvent(
            subagent_id=subagent_id,
            tool=tool,
            args=args,
            seq=seq,
            parent_tool_call_id=GC_TOOL_CALL,
        ),
        **overrides,
    )


def thought(
    text: str,
    *,
    seq: int,
    subagent_id: str = GC_ID,
    **overrides: object,
) -> _ThoughtEvent:
    """One accumulated thinking block (omp-side only, spec D5)."""
    return _merge(_ThoughtEvent(subagent_id=subagent_id, text=text, seq=seq), **overrides)


def message(
    role: str,
    text: str,
    *,
    seq: int,
    subagent_id: str = GC_ID,
    **overrides: object,
) -> _MessageEvent:
    """A completed assistant/user message (text blocks only)."""
    return _merge(
        _MessageEvent(subagent_id=subagent_id, role=role, text=text, seq=seq), **overrides
    )


# ---------------------------------------------------------------------------
# ScriptedTimeline — the canned scenario, deterministic by construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimedEvent:
    """One replay beat: sim-time seconds since scenario start + the event."""

    t: float
    event: SimEvent


class ScriptedTimeline:
    """Immutable replay of the canned 3-deep fan-out story.

    Two independently constructed instances are equal — no clock, no
    randomness, no environment reads. ``t`` values are the deterministic
    schedule the driver paces against when replaying in real time.
    """

    def __init__(self) -> None:
        events: List[TimedEvent] = []

        def at(t: float, event: SimEvent) -> None:
            events.append(TimedEvent(t=t, event=event))

        # -- discovery stream: engine-wide seq 1..N in emission order ------
        d = 0

        def dseq() -> int:
            nonlocal d
            d += 1
            return d

        # -- omp_feed stream: feed-wide seq 1..M in emission order ---------
        m = 0

        def mseq() -> int:
            nonlocal m
            m += 1
            return m

        # 0.0s — gateway spawns the orchestrator (discovery child of the
        # gateway session; gw-space parity renders it nested under the
        # gateway agent's subspace as the fan-out root).
        at(0.0, node_add(
            ORCH_NAME, ORCH_GOAL,
            parent_session=GATEWAY_SESSION,
            delegation_id=ORCH_DELEGATION,
            task_index=0,
            seq=dseq(),
            child_session_id=ORCH_SESSION,
        ))

        # 1.0s — orchestrator fans out two children (one delegation, two tasks).
        at(1.0, node_add(
            CHILD_A_NAME, CHILD_A_GOAL,
            parent_session=ORCH_SESSION,
            delegation_id=CHILD_DELEGATION,
            task_index=0,
            seq=dseq(),
            child_session_id=CHILD_A_SESSION,
            child_subagent_id=f"{CHILD_DELEGATION}/0",
        ))
        at(1.5, node_add(
            CHILD_B_NAME, CHILD_B_GOAL,
            parent_session=ORCH_SESSION,
            delegation_id=CHILD_DELEGATION,
            task_index=1,
            seq=dseq(),
            child_session_id=CHILD_B_SESSION,
            child_subagent_id=f"{CHILD_DELEGATION}/1",
        ))

        # 3.0s — child B (an omp RPC child) spawns one in-process grandchild.
        at(3.0, gc_add(seq=mseq()))

        # 4.0–12.0s — the grandchild works: tools, thinking, a steer, a reply.
        at(4.0, thought(
            "The renderer keys rooms by node id; the plan diff must treat "
            "discovery and omp_feed adds as the same lifecycle verb.",
            seq=mseq(),
        ))
        at(4.5, tool_call(
            "bash",
            "python -m pytest tests/observatory/test_sim.py -q",
            seq=mseq(),
        ))
        at(6.0, thought(
            "Deaths arrive out of order across feeds — the renderer must "
            "tolerate a grandchild death landing before its parent's.",
            seq=mseq(),
        ))
        at(6.5, tool_call(
            "grep",
            "pattern=death path=hermes/observatory",
            seq=mseq(),
        ))
        at(8.0, message(
            "user",
            "[steer] also cover the quiet gap while the orchestrator idles",
            seq=mseq(),
        ))
        at(10.0, tool_call(
            "edit",
            "hermes/tests/observatory/test_sim.py 18.=24:",
            seq=mseq(),
        ))
        at(12.0, message(
            "assistant",
            "Timeline is deterministic: replayed twice, byte-identical event "
            "streams, deaths strictly ordered grandchild → children → root.",
            seq=mseq(),
        ))

        # 13.0s — grandchild settles.
        at(13.0, gc_death(seq=mseq()))

        # 14.0s / 15.0s — the two children settle (B outlives its grandchild).
        at(14.0, node_death(
            parent_session=ORCH_SESSION,
            delegation_id=CHILD_DELEGATION,
            task_index=0,
            seq=dseq(),
            child_session_id=CHILD_A_SESSION,
            child_subagent_id=f"{CHILD_DELEGATION}/0",
            summary="testhome provisions offline: version file gates the fetch",
        ))
        at(15.0, node_death(
            parent_session=ORCH_SESSION,
            delegation_id=CHILD_DELEGATION,
            task_index=1,
            seq=dseq(),
            child_session_id=CHILD_B_SESSION,
            child_subagent_id=f"{CHILD_DELEGATION}/1",
            summary="renderer streams 4 event types; rooms verified",
        ))

        # 16.0–22.0s — the orchestrator stays live (scheduled quiet gap; the
        # renderer's dashboard must show it live here).
        at(22.0, node_death(
            parent_session=GATEWAY_SESSION,
            delegation_id=ORCH_DELEGATION,
            task_index=0,
            seq=dseq(),
            child_session_id=ORCH_SESSION,
            summary="M3c E2E gate green: fan-out rendered, all deaths ordered",
        ))

        self._events: tuple[TimedEvent, ...] = tuple(events)

    @property
    def events(self) -> tuple[TimedEvent, ...]:
        return self._events

    def __iter__(self):
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ScriptedTimeline) and self._events == other._events

    def __hash__(self) -> int:  # pragma: no cover - convenience only
        return hash(self._events)

    def duration(self) -> float:
        """Sim-time span of the scenario (0 for an empty timeline)."""
        return self._events[-1].t if self._events else 0.0

    def story(self) -> str:
        """One line per beat: ``<t> <EventClass> <identifying fields>``."""
        lines = []
        for te in self._events:
            ev = te.event
            cls = type(ev).__name__
            if isinstance(ev, _DiscoveryNodeEvent):
                who = ev.name or f"{ev.delegation_id}/{ev.task_index}"
                ident = f"kind={ev.kind} {who!r} parent={ev.parent_session}"
            elif isinstance(ev, _OmpNodeEvent):
                ident = f"kind={ev.kind} {ev.subagent_id} status={ev.status}"
            elif isinstance(ev, _ToolEvent):
                ident = f"{ev.subagent_id} {ev.tool}({(ev.args or '')[:40]})"
            elif isinstance(ev, _ThoughtEvent):
                ident = f"{ev.subagent_id} {ev.text[:40]}…"
            else:
                ident = f"{ev.subagent_id} {ev.role}: {ev.text[:40]}"
            lines.append(f"{te.t:6.1f}s {cls:12s} {ident}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Driver — feed a timeline into any callable (the renderer's ingest)
# ---------------------------------------------------------------------------


def drive(
    timeline: ScriptedTimeline | Iterable[TimedEvent],
    ingest: Ingest,
    *,
    realtime: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> list:
    """Replay ``timeline`` into ``ingest(event)``; returns the fed events.

    ``realtime=True`` paces the replay on the deterministic schedule (the
    delta between consecutive ``t`` values) using ``sleeper`` — the M3c E2E
    gate uses this against a live renderer; tests replay instantly.
    """
    events = timeline.events if isinstance(timeline, ScriptedTimeline) else timeline
    fed: list = []
    prev_t = 0.0
    for beat in events:
        if realtime:
            delay = beat.t - prev_t
            if delay > 0:
                sleeper(delay)
            prev_t = beat.t
        ingest(beat.event)
        fed.append(beat.event)
    return fed


# ---------------------------------------------------------------------------
# CLI — smoke/E2E entry: print the story, or feed a dotted-path ingest
# ---------------------------------------------------------------------------


def _import_dotted(dotted: str) -> Ingest:
    module_name, _, attr = dotted.partition(":")
    if not module_name or not attr:
        raise SystemExit(f"--ingest expects 'package.module:callable', got {dotted!r}")
    import importlib

    obj = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj  # type: ignore[no-any-return]


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m observatory.sim",
        description="Replay the canned observatory event scenario (deterministic).",
    )
    parser.add_argument(
        "--ingest", default=None, metavar="pkg.mod:callable",
        help="feed each event into this callable (the renderer's ingest); "
             "default: print the story",
    )
    parser.add_argument(
        "--realtime", action="store_true",
        help="pace the replay on the scripted sim-time schedule",
    )
    args = parser.parse_args(argv)

    timeline = ScriptedTimeline()
    if not args.ingest:
        print(timeline.story())
        return 0
    drive(timeline, _import_dotted(args.ingest), realtime=args.realtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
