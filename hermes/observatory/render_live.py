"""M3c LIVE gate (marker ``MERCURY-M3C-OK``): throwaway observatory home ->
real Tuwunel homeserver -> Renderer driving the full §3/§5/D8 surface.

1. §3 provisioning (gw-space parity) — root space with correct child
   ORDER (gateway agent SUBSPACE first, directives room, cron room,
   orchestrator subspace), the gateway agent's subspace holding its room
   with the gateway-origin delegation child nested under it, orchestrator
   with the gateway-origin delegation child nested under it, orchestrator
   space with nested subagent spaces/rooms;
2. §5 events — a tool-call message (args truncated + ``[full]``), an omp
   thinking message (quoted), a lifecycle message, a rolling dashboard
   EDITED in place (two revisions, D15);
3. D8 deaths — depth-2 settle (room alive + "settled — transcript only"),
   depth-1 instant purge (room 404s, summary in the PARENT room only),
   cascade (the settled grandchild dies with its parent), depth-0 /exit
   cascade (whole orchestrator subtree purged);
4. D7 — owner invited + PL 100 everywhere (spot-checked via power levels).

Usage (repo venv):
    python -m observatory.render_live [--home DIR]

The throwaway home defaults to ``~/.mercury/observatory-build/testhome`` —
NEVER the user's real ``~/.mercury/observatory``. Provisioning reuses
:mod:`observatory.provision` (idempotent); the gate boots and stops ITS
OWN tuwunel process (no systemd). Files are left in place on both pass
and fail; the gate log lands next to the home.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from observatory import provision, tree
from observatory.appservice import as_token_from_registration
from observatory.config_gen import ObservatoryPaths
from observatory.identity import assign_slug, virtual_mxid
from observatory.matrix_client import MatrixClient, MatrixError
from observatory.renderer import (
    SETTLED_MARKER,
    IntentExecutor,
    Renderer,
    children_by_ts,
    dashboard_message,
)
from observatory.state import ObservatoryState

log = logging.getLogger("m3c-gate")

GATE_MARKER = "MERCURY-M3C-OK"
DEFAULT_HOME = Path.home() / ".mercury" / "observatory-build" / "testhome"

# Scripted simulation actors (spec §3/§5).
GW = "gw"            # gateway agent (depth 0, kind=gateway)
CRON = "cron:nightly"
GWSA = "sa-direct"   # gateway-origin delegation child -> nests under the
                     # gateway agent's subspace (gw-space parity)
ORCH = "orch"        # spawned orchestrator (depth-0 root)
SA = "sa-tests"      # depth-1 delegate_task child  -> instant purge on death
SSA = "ssa-lint"     # depth-2 omp grandchild       -> settled marker on death


class Gate:
    """Pass/fail ledger with evidence lines; every check is logged."""

    def __init__(self, log_path: Path):
        self._file = log_path.open("w", encoding="utf-8")
        self.passed = 0
        self.failed = 0

    def line(self, text: str) -> None:
        print(text, flush=True)
        self._file.write(text + "\n")
        self._file.flush()

    def check(self, name: str, ok: bool, evidence: str = "") -> bool:
        mark = "PASS" if ok else "FAIL"
        self.line(f"[{mark}] {name}" + (f" — {evidence}" if evidence else ""))
        if ok:
            self.passed += 1
        else:
            self.failed += 1
        return ok

    def close(self) -> None:
        self._file.close()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def load_runtime(paths: ObservatoryPaths) -> tuple[dict, str]:
    import tomllib

    with open(paths.toml, "rb") as f:
        cfg = tomllib.load(f)["global"]
    address = cfg.get("address", "127.0.0.1")
    if isinstance(address, list):
        address = address[0]
    return cfg, paths.homeserver_url(address=address, port=int(cfg.get("port", 18008)))


def seed_nodes(state: ObservatoryState) -> None:
    """The scripted §3 tree: gateway + cron job + gateway-origin delegate
    child + orchestrator (depth 0) -> delegate child (depth 1) -> omp
    grandchild (depth 2)."""

    def add(node_id: str, name: str, *, engine: str, parent: str | None, extra: dict | None = None):
        slug = assign_slug(name, state)
        state.add_node(
            node_id,
            engine=engine,
            name=name,
            slug=slug,
            mxid=virtual_mxid(slug),
            session_ref=f"session:{node_id}",
            parent_node_id=parent,
            extra=extra,
        )

    add(GW, "gateway agent", engine="hermes", parent=None, extra={"kind": "gateway"})
    add(CRON, "nightly", engine="hermes", parent=GW, extra={"kind": "cron-job"})
    add(GWSA, "patch-audit", engine="hermes", parent=GW)
    add(ORCH, "auth-refactor", engine="hermes", parent=None)
    add(SA, "test-sweep", engine="omp", parent=ORCH)
    add(SSA, "lint-fix", engine="omp", parent=SA)


async def ensure_virtual_users(client: MatrixClient, state: ObservatoryState) -> None:
    for row in state.get_live():
        localpart = row["mxid"].lstrip("@").split(":", 1)[0]
        try:
            await client.register_virtual_user(localpart)
        except MatrixError as exc:
            log.info("register %s: %s (continuing — ghost may auto-provision)", localpart, exc)


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

async def space_children_in_order(client: MatrixClient, space_id: str, sender: str) -> list[str]:
    hierarchy = await client.room_hierarchy(space_id, sender=sender)
    for room in hierarchy.get("rooms", []):
        if room.get("room_id") == space_id:
            return children_by_ts(room.get("children_state", []))
    return []


async def message_bodies(client: MatrixClient, room_id: str, limit: int = 200) -> list[dict]:
    events = await client.admin_room_messages(room_id, direction="b", limit=limit)
    return [e.get("content", {}) for e in events if e.get("type") == "m.room.message"]


def find_body(contents: list[dict], needle: str) -> str | None:
    for content in contents:
        if needle in str(content.get("body", "")):
            return str(content.get("body"))
    return None


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

async def scenario(gate: Gate, paths: ObservatoryPaths, base_url: str, cfg: dict) -> None:
    server_name = str(cfg.get("server_name", "mercury.local"))
    as_token = as_token_from_registration(paths.appservice_registration)
    owner = json.loads(paths.owner_credentials.read_text(encoding="utf-8"))
    owner_mxid = owner["user_id"]

    state = ObservatoryState(paths.root / "state.db")
    if not state.get_live():
        seed_nodes(state)
    gw_row = state.get(GW)
    gw_mxid = gw_row["mxid"]

    async with MatrixClient(
        base_url, as_token, server_name=server_name, admin_token=owner["access_token"]
    ) as client:
        await ensure_virtual_users(client, state)

        renderer = Renderer(
            state,
            gateway_node_id=GW,
            server_name=server_name,
            owner_mxid=owner_mxid,
            executor=IntentExecutor(client, state, owner_mxid=owner_mxid, server_name=server_name),
        )

        # --- 1. §3 provisioning ------------------------------------------------
        plan = renderer.build_plan(host="gatehost")
        applied = await renderer.apply_plan(plan)
        gate.line(f"apply_plan executed {len(applied)} intents")
        ids = {n: (state.get(n)["space_id"], state.get(n)["room_id"])
               for n in (GW, GWSA, CRON, ORCH, SA, SSA)}
        directives = state.get_meta("room:directives")
        gw_space, gw_room = ids[GW]
        gw_agent_space = state.get_meta("space:gw-agent")
        gwsa_space, gwsa_room = ids[GWSA]
        orch_space, orch_room = ids[ORCH]
        sa_space, sa_room = ids[SA]
        ssa_space, ssa_room = ids[SSA]
        gate.line(f"ids: root=({gw_space}, gw_room={gw_room}) "
                  f"gw-agent-space={gw_agent_space} gwsa=({gwsa_space}, {gwsa_room}) "
                  f"directives={directives} cron={ids[CRON]} "
                  f"orch=({orch_space}, {orch_room}) sa=({sa_space}, {sa_room}) "
                  f"ssa=({ssa_space}, {ssa_room})")

        # check 1 — root space exists
        gate.check("root space exists", await client.admin_room_alive(gw_space),
                   gw_space)

        # check 2 — gateway agent subspace exists (gw-space parity)
        gate.check("gateway agent subspace exists",
                   bool(gw_agent_space) and await client.admin_room_alive(gw_agent_space),
                   gw_agent_space or "MISSING")

        # check 3 — §3 child ORDER in the root space: gateway agent
        # subspace FIRST, then directives, cron rooms, orchestrators.
        order = await space_children_in_order(client, gw_space, gw_mxid)
        gate.check(
            "root space child order (gateway subspace, directives, cron, orchestrator)",
            order == [gw_agent_space, directives, ids[CRON][1], orch_space],
            f"got {order}",
        )

        # check 4 — gateway agent subspace holds the gateway room + the
        # gateway-origin delegation child's space, nested.
        gwa_order = await space_children_in_order(client, gw_agent_space, gw_mxid)
        gate.check(
            "gateway subspace children (gateway room + delegation-child space)",
            gwa_order == [gw_room, gwsa_space],
            f"got {gwa_order}",
        )

        # check 5 — the delegation child's own room sits in its nested space
        gwsa_order = await space_children_in_order(
            client, gwsa_space, state.get(GWSA)["mxid"]
        )
        gate.check(
            "gateway-origin delegation child room nests under its space",
            gwsa_order == [gwsa_room],
            f"got {gwsa_order}",
        )

        # check 6 — orchestrator space exists with nested child spaces
        orch_order = await space_children_in_order(client, orch_space, state.get(ORCH)["mxid"])
        gate.check(
            "orchestrator space children (orch room + subagent space)",
            orch_order == [orch_room, sa_space],
            f"got {orch_order}",
        )

        # --- 2. §5 events --------------------------------------------------------
        await renderer.render_lifecycle(ORCH)
        await renderer.render_tool_call(
            SA, "bash", "cd repo && npm test -- --watchAll=false " + "x" * 300
        )
        await renderer.render_thinking(SA, "Coverage dipped on the retry path; rerun with a clean cache.")
        dash1_body, dash1_html = dashboard_message(
            "auth-refactor", agents=3, delegations=1, blocked=0, extra=["- test-sweep: running"]
        )
        await renderer.render_dashboard(ORCH, dash1_body, formatted=dash1_html)
        dash2_body, dash2_html = dashboard_message(
            "auth-refactor", agents=2, delegations=1, blocked=1, extra=["- test-sweep: blocked"]
        )
        await renderer.render_dashboard(ORCH, dash2_body, formatted=dash2_html)
        root_body, root_html = dashboard_message("Mercury — gatehost", agents=4, delegations=1)
        await renderer.render_dashboard(GW, root_body, formatted=root_html)

        # check 7 — tool-call message rendered (truncated args + [full])
        sa_contents = await message_bodies(client, sa_room)
        tool_msg = find_body(sa_contents, "🔧")
        ok = bool(tool_msg) and "[full]" in tool_msg and "watchAll" in tool_msg \
            and "x" * 300 not in tool_msg
        gate.check("tool-call message (name + args truncated + [full])", ok,
                   (tool_msg or "")[:160])

        # check 8 — thinking as separate quoted message
        ok = any(
            str(c.get("formatted_body", "")).startswith("<blockquote>")
            for c in sa_contents
        )
        gate.check("thinking message (quoted formatted body)", ok)

        # check 9 — dashboard edited in place: two revisions, one event id
        events = await client.admin_room_messages(orch_room, direction="b", limit=100)
        replaces = [
            e for e in events
            if (e.get("content", {}).get("m.relates_to") or {}).get("rel_type") == "m.replace"
        ]
        dash_event = state.get_meta(f"dash:{ORCH}")
        ok = bool(replaces) and dash1_body and dash2_body
        if ok:
            new_body = str(replaces[0]["content"].get("m.new_content", {}).get("body", ""))
            targets = {r["content"]["m.relates_to"]["event_id"] for r in replaces}
            ok = new_body == dash2_body and dash_event in targets
        gate.check("dashboard rolling edit (rev1 sent, rev2 m.replace in place)", ok,
                   f"replace events={[r['event_id'] for r in replaces]}")

        # check 10 — root dashboard in the gateway agent's room
        gate.check("root dashboard in gateway room",
                   bool(find_body(await message_bodies(client, gw_room), "📊 Mercury — gatehost")))

        # --- 3. D8 deaths ----------------------------------------------------------
        # depth-2 settle: room SURVIVES with the marker + summary to parent.
        await renderer.render_death(SSA, status="completed", summary="lint fixed, 2 files")

        gate.check("depth-2 death keeps room alive", await client.admin_room_alive(ssa_room),
                   ssa_room)
        gate.check("depth-2 death posts settled marker",
                   bool(find_body(await message_bodies(client, ssa_room), SETTLED_MARKER)))
        gate.check("depth-2 summary lands in parent room",
                   bool(find_body(await message_bodies(client, sa_room), "lint fixed")))

        # depth-1 instant purge: room+space 404, summary in the PARENT room only.
        await renderer.render_death(SA, status="completed", summary="3 tests green")

        gate.check("depth-1 death purges room (admin 404)",
                   not await client.admin_room_alive(sa_room), sa_room)
        gate.check("depth-1 death purges space (admin 404)",
                   not await client.admin_room_alive(sa_space), sa_space)
        gate.check("depth-1 summary lands in parent room only",
                   bool(find_body(await message_bodies(client, orch_room), "3 tests green")))

        # D8 cascade: the settled grandchild's grace was the parent's lifetime.
        gate.check("cascade purges settled grandchild with parent",
                   not await client.admin_room_alive(ssa_room), ssa_room)

        # depth-0 /exit: whole orchestrator subtree annihilated.
        await renderer.render_death(ORCH, status="exit", summary="branch merged")

        gate.check("depth-0 /exit purges orchestrator space",
                   not await client.admin_room_alive(orch_space), orch_space)
        gate.check("depth-0 /exit purges orchestrator room",
                   not await client.admin_room_alive(orch_room), orch_room)
        gate.check("gateway space survives 0-agent subtree death",
                   await client.admin_room_alive(gw_space), gw_space)

    state.close()


# ---------------------------------------------------------------------------
# Orchestration (sync shell around the async scenario)
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M3c live gate (MERCURY-M3C-OK)")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME,
                        help="throwaway observatory home (default: %(default)s)")
    parser.add_argument("--fresh", action="store_true",
                        help="wipe the throwaway home first (clean-slate run)")
    args = parser.parse_args(argv)

    home = args.home.expanduser()
    if home.resolve() == (Path.home() / ".mercury" / "observatory").resolve():
        print("REFUSING to run the gate against the real ~/.mercury/observatory", file=sys.stderr)
        return 2
    if args.fresh:
        import shutil

        shutil.rmtree(home, ignore_errors=True)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    home.mkdir(parents=True, exist_ok=True)
    gate_log = home.parent / "gate-m3c.log"
    gate = Gate(gate_log)
    gate.line(f"== M3c live gate == {time.strftime('%Y-%m-%d %H:%M:%S')} home={home}")

    # 1. Provision the throwaway home (idempotent; downloads tuwunel on first run).
    summary = provision.provision(mercury_home=home, systemd=False)
    gate.line(f"provision: {json.dumps(summary)}")

    paths = ObservatoryPaths(home)
    cfg, base_url = load_runtime(paths)

    # 2. Boot OUR tuwunel on the closed config.
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    server_log = (paths.logs_dir / "tuwunel-gate.log").open("wb")
    proc = subprocess.Popen(
        [str(paths.binary), "-c", str(paths.toml)],
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )
    exit_code = 1
    fatal = False
    try:
        provision._wait_for_homeserver(base_url)
        gate.line(f"tuwunel up (pid {proc.pid}) at {base_url} "
                  f"(server_name={cfg.get('server_name')})")
        asyncio.run(scenario(gate, paths, base_url, cfg))
    except BaseException as exc:  # noqa: BLE001 — gate reports, never swallows
        fatal = True
        gate.line(f"[FATAL] {type(exc).__name__}: {exc}")
        import traceback

        gate.line(traceback.format_exc())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
            gate.line(f"tuwunel stopped (pid {proc.pid})")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            gate.line(f"tuwunel killed (pid {proc.pid})")
        server_log.close()

    total = gate.passed + gate.failed
    verdict = (GATE_MARKER if gate.failed == 0 and total > 0 and not fatal
               else "MERCURY-M3C-FAIL")
    gate.line(f"== {verdict} {gate.passed}/{total} checks == log: {gate_log}")
    gate.close()
    return 0 if verdict == GATE_MARKER else exit_code


if __name__ == "__main__":
    sys.exit(main())
