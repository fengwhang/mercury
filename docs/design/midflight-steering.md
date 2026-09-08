# Mid-flight steering of omp delegation batches — design investigation

Status: INVESTIGATION (2026-09-08, omp delegate on hermes' request). No code
changes; this doc records verified current mechanics, three candidate designs,
risks, and a recommendation. Truth source for any implementation.

Scope: the ORCHESTRATOR'S ORCHESTRATOR — the human user chatting with the
hermes gateway/CLI session that dispatched a `delegate_task` fan-out — gaining
the ability to steer, stop, re-prioritize, or append tasks to that batch while
it runs. The omp RPC protocol already has steer/abort (`rpc-mode.ts` dispatch,
see §1.2); hermes already forwards them (§1.2). This is a Hermes-side
orchestration-UX question, not a protocol question.

## 0. Problem statement

When hermes dispatches a top-level `delegate_task` batch, the fan-out runs in
the background and the conversation is free — but the only actor that can
course-correct the running children is the hermes MODEL, mid-turn, via a
`delegate_task(action='steer')` tool call. The user in the chat has no verb
that reaches the children:

- While hermes' own turn is still running, a user follow-up is consumed by
  the busy-input path (interrupt/queue/steer — §1.3) and either interrupts the
  PARENT, waits for the turn boundary, or steers the PARENT. None of these
  reaches the omp children.
- After hermes' turn ends (batch still running), a user follow-up starts a
  FULL new agent turn: one model round-trip in which hermes must remember the
  `subagent_id`s and choose to call the control action. Latency + token cost +
  compliance risk.
- `/stop` while idle does not touch background batches at all (§1.3); the only
  user-visible kill is `/new`, which also resets the conversation (§1.3).

Everything needed to close this already exists as machinery: live-child
registry with ownership, RPC steer/abort, busy-slash dispatch with
`busy_policy`, per-session delegation records. The gap is wiring, not
capability.

## 1. Current mechanics (verified against code)

### 1.1 What hermes retains when it dispatches a background batch

Spawn path: `dispatch_omp_delegation` (`hermes/tools/omp_delegation.py:1006`)
builds one `delegation_id = deleg_<hex8>` (:1098), stamps
`owner_session_id` = the spawning agent's durable session id (:1099), and —
for a top-level (depth-0) delegation — hands the whole fan-out to
`dispatch_async_delegation_batch` (:1153-1171) with:

- `runner` = a closure over `_sync_run(...)` (:1162) that spawns every child
  and blocks until ALL finish;
- `interrupt_fn` = `_interrupt_batch` (:1120-1121) which SIGKILLs exactly this
  batch's `batch_procs` via `_kill_procs` (`omp_delegation.py:113-131`);
- `session_key` / `origin_ui_session_id` / `parent_session_id` routing stamps.

Handles retained in-process:

| Store | Shape | Citations |
|---|---|---|
| `omp_delegation._live_children` | `<delegation_id>/<task_index>` → `{child_id, delegation_id, task_index, name, goal, model, owner_session_id, transport_kind, steerable, transport, started_at, stop_requested}`; `transport` is the live `OmpRpcChild` (steerable) or one-shot `Popen` (kill-only) | `omp_delegation.py:139-167, 743-751, 800-803` |
| `omp_delegation._live_procs` | global transport list (counts, legacy kill) | `omp_delegation.py:87-88, 155-157` |
| `async_delegation._records[delegation_id]` | batch record incl. `status`, `interrupt_fn`, `progress_fn`, `session_key`, `origin_ui_session_id`, `parent_session_id`, `names`, `goals`; mirrored durably into `state.db` table `async_delegations` | `tools/async_delegation.py:1068-1109, 244-276` |
| `process_registry.completion_queue` | where the batch's single combined completion event lands; the gateway's `_async_delegation_watcher` thread (started at boot, `gateway/run.py:14368`) drains it and injects it as a new turn | `async_delegation.py:1158-1236`; `gateway/run.py:27803-27860, 22440-22444` |

The `delegate_task` tool RESULT (what hermes' model sees) includes the
addressable child list — `children: [{subagent_id, name, goal}]` — plus the
note "While it runs you can steer it (action='steer' + subagent_id + message)
or stop it early (action='stop')" (`omp_delegation.py:1172-1204`).

### 1.2 What `delegate_task action=steer/stop` reaches TODAY (M0A / observatory Phase 1 wiring)

`dispatch_omp_delegation` routes `action ∈ {list, steer, stop}` to
`handle_omp_control_action` (`omp_delegation.py:1016-1023, 213-345`):

- **list** — live children with id/name/goal/transport/steerable/running
  seconds, filtered to the caller's spawn tree (:232-264).
- **steer** — resolves the id (`_resolve_live_child` accepts the full
  `<delegation_id>/<task_index>` or a bare `<delegation_id>` when exactly one
  child is live, :170-195), enforces ownership (`_owns_live_child`: only the
  spawning session steers, :198-210), then `transport.steer(text)` →
  RpcClient `steer` → omp server `session.steer(command.message)`
  (`tools/omp_rpc_transport.py:390-396`; `omp/packages/coding-agent/src/modes/rpc/rpc-mode.ts:1271-1273`).
  omp injects the text at the next safe boundary; the in-flight tool call is
  never cut. One-shot fallback children answer honestly that they cannot be
  steered (`omp_delegation.py:290-295`).
- **stop** — `transport.abort(reason=...)` (graceful boundary) with SIGKILL
  fallback on connection loss (:316-341); sets `stop_requested` on the record.

So: mid-batch steering EXISTS end-to-end, but its only entry point is the
model-facing `delegate_task` schema (`tools/delegate_tool.py:5229-5254`,
advertised at :5046-5048 and :4588-4591). No user surface calls
`handle_omp_control_action`.

Dead path worth knowing: the TUI's WS method `subagent.steer`
(`tui_gateway/methods_session.py:3443-3486`) and `tools/delegate_tool.steer_subagent`
(:347-392) target the LEGACY in-process registry `_active_subagents`
(populated only by the Mercury-child spawn path, `delegate_tool.py:2794-2795`,
which the model-facing path no longer uses — `omp_delegation.py:3-6`). They do
NOT reach omp children registered in `_live_children`.

### 1.3 Why a USER message mid-turn cannot reach a running batch

Two distinct windows:

**(i) hermes' turn still active** (e.g. it dispatched the batch and is doing
other tool calls). Inbound messages hit `_handle_active_session_busy_message`
(`gateway/run.py:10881`) and resolve per `busy_input_mode`
(`interrupt|queue|steer`, :11029-11141):

- `steer` → `running_agent.steer(text)` — AIAgent-level: the text is stashed
  and appended to the PARENT's last tool result at the next iteration
  (`run_agent.py:3545-3579`). It reaches hermes' own loop, not the children.
- `queue` → `_queue_or_replace_pending_event` FIFO (:11136-11137); replayed as
  the next turn after the current run.
- `interrupt` → `running_agent.interrupt()`; explicitly DEMOTED to queue when
  the agent has active subagents (#30170, :11040-11058, oracle at :10677) so
  a follow-up "doesn't destroy minutes of subagent work". Note the oracle
  inspects the legacy `_active_children` list; background omp batches are NOT
  in it — but a parent-turn interrupt doesn't kill them either, because the
  batch is detached (the tool call already returned).

**(ii) hermes idle, batch running** (the common long window). The batch holds
no session lock — dispatch returned immediately and the runner lives on the
daemon executor (`async_delegation.py:1110-1147`). A user message simply
starts a NEW agent turn. Nothing in the intake path knows the session owns a
live delegation, so nothing routes the text anywhere except into a fresh model
call where hermes must itself decide to call `delegate_task(action='steer')`.

Consequence for `/stop`: with no running agent, `_handle_stop_command` clears
the running-agent map and typing indicators and replies "no active"
(`gateway/slash_commands.py:1314-1395`) — background batches are untouched.
The paths that DO kill a session's batches: `/new`/`/reset`
(`_handle_reset_command` → `interrupt_for_session(reason="session_reset")`,
`slash_commands.py:218-224`), TUI session finalize
(`tui_gateway/server.py:1018-1034`), gateway shutdown (`run.py:15991-15994`).
`interrupt_for_session` (`async_delegation.py:1541-1595`) invokes each record's
`interrupt_fn` — a hard SIGKILL of the batch's children, after which the
worker's `finally` still finalizes and delivers whatever per-child entries
survive (`async_delegation.py:1112-1135`).

### 1.4 Partial finalization: what exists today

- A batch finalizes exactly once, after ALL children return
  (`_worker` finally → `_finalize_batch`, `async_delegation.py:1112-1168`).
  There is no "finalize N of M now, keep the rest running" API.
- Killing children does produce a completion: SIGKILL → each child's
  `run_task`/one-shot catch returns `{status: failed, summary: None, error:
  ...}` (`omp_rpc_transport.py:360-374`), so the consolidated re-entry lists
  killed children WITHOUT their partial work. Graceful RPC `abort` stops the
  child at a boundary and the stop note promises "Its partial result still
  re-enters the conversation as a completion message"
  (`omp_delegation.py:331-341`).
- The stale monitor is the only force-finalize path today (progress-based,
  interrupt → grace → terminal `stalled` event, `async_delegation.py:94-121,
  1239-1418`).

## 2. Precedent check: the gateway ALREADY has "command while busy" mechanics

The slash-command registry declares per-command mid-run behavior
(`mercury_cli/commands.py:99-140`): `busy_policy ∈ {dispatch, reject,
interrupt_then_dispatch}` plus an optional `busy_handler` naming a special
mid-run variant in the gateway's single Guard-2 dispatcher table
`_dispatch_busy_slash_command` (`gateway/run.py:17741-17842`, special table at
:17810-17822). Existing examples that legally run while a turn is live:

- `/steer <prompt>` — `busy_policy="dispatch", busy_handler="steer"`
  (`commands.py:213-214`) → `_busy_steer_command` (`run.py:17961-18008`) →
  `running_agent.steer()` — injects into the parent's next tool result,
  "no interrupt, no new user turn". **This is the parent-level steer; it does
  not reach omp children.** A future child-steering verb must NOT reuse the
  bare `/steer` name.
- `/queue` (`busy_handler="queue"`, FIFO enqueue, `run.py:17920-17959`),
  `/agents` (`busy_policy="dispatch"` — lists live delegations read-only from
  `list_async_delegations()` while a turn runs, `slash_commands.py:1163-1312`,
  async section :1248-1259), `/approve` `/deny` `/pause` `/bg` (all dispatch).
- Busy TEXT (non-slash) follows `busy_input_mode` per platform, including a
  steer mode for plain follow-ups (`run.py:11072-11104`, ack copy :11193-11248).

Key precedent conclusion: a slash command registered with
`busy_policy="dispatch"` runs in BOTH windows — mid-turn via Guard-2 and
idle-batch via normal dispatch (the idle handler in the
`GatewaySlashCommandsMixin`). The idle-batch window needs no busy mechanics
at all because the batch holds no session lock (§1.3-ii).

Other in-repo prior art:

- **Legacy in-process engine**: `steer_subagent` + `interrupt_subagent`
  (`delegate_tool.py:323-392`) queue text at iteration boundaries with an
  ownership chain and `missed_steer` drain for text that arrives after the
  last boundary. The M0A control plane is the omp-transport analogue.
- **omp collab protocol**: guest→host `agent-cmd` frames carry live
  `chat`/`kill`/`revive` against named agents
  (`omp/packages/coding-agent/src/collab/protocol.ts:64-67`,
  `guest.ts:204-214`) — proof the underlying engine treats named mid-run
  steering as a first-class verb.
- **Matrix observatory control router**: `observatory/control.py` models the
  whole intent space — `parse_intent` (SteerText/SidecarVerb/EngineCommand,
  :142-188), class-routed actions (`InjectText`, `OmpSteer`, `OmpPrompt`,
  `OmpAbortMain`, `OmpSubagentSteer`, `OmpSubagentAbort`, :258-340), a
  queued/applied honesty ledger with a per-node unconfirmed-steer cap
  (`STEER_QUEUE_CAP_DEFAULT = 64`, :86-89) and stop boundary-wait states
  (:91-96). Currently the SIDECAR's ingestion layer only; its engine
  transports are logged, not executed, pending M4a/M5 gateway-side wiring
  (`observatory/sidecar_main.py:43-45`).

### What observatory D12/D13 already covers (do not duplicate)

- **D12 directives room** (`docs/design/matrix-observatory.md:44, §6:180-201`):
  owner → selected 0-AGENTS only, @-mention-gated, Matrix-side. Delivery:
  "new turn if idle; queued after the current turn if busy" for hermes-side
  agents; RPC steer for omp-side. Not delegation children, not other surfaces.
- **D13 commands** (:45): `/verb`+`!verb` sidecar vocab, command scope by
  agent class, everything else routed to the agent's NATIVE surface — "hermes
  rooms → gateway slash dispatch (full registry)". So the observatory design
  EXPECTS the gateway slash registry to be the native hermes-side command
  surface; a gateway verb is complementary, not competing.
- **Observatory Phase 4 / M4a** (:292-294, `sidecar_main.py:43-45`): the
  Matrix-side steering UX (queued/applied, `/stop`, approvals) against the
  SAME live children — via the gateway's registry handles (the sidecar boots
  on the gateway thread and adopts its live handles,
  `sidecar_main.py:517-535`, `platform_hook.py:18-20`). When it lands, users
  in Element can steer these children; users in Telegram/WhatsApp/CLI/TUI
  still cannot. THIS doc designs that native-surface path and deliberately
  reuses the same control plane (`handle_omp_control_action`) so both
  surfaces stay consistent.

## 3. Candidate designs

### Design A — interrupt-and-requeue (emergency re-prioritization)

Inbound user message (or an explicit verb) while a batch is live: stop the
batch's children, collect partials, re-enter the conversation with
[partials + the user's new instruction], and let hermes re-dispatch a
corrected batch.

Mechanics available today:

1. Stop per child: `handle_omp_control_action('stop', child_id, ...)` loops
   are already per-child; a batch-scoped variant iterates `_live_children`
   entries with the `delegation_id` prefix and calls `transport.abort()`
   (graceful → partial summaries survive) or the record's `interrupt_fn`
   (SIGKILL → summaries lost, §1.4).
2. Finalization: the worker's `finally` already finalizes and pushes ONE
   combined completion event once every child returns
   (`async_delegation.py:1112-1135`). Nothing new needed for collection.
3. Re-entry ordering: the completion event and the user's message are two
   independent queue paths (completion via `_async_delegation_watcher`;
   user text via the normal intake / FIFO) — see risk R3.

Trade-offs:

- **(+)** Simplest mental model; the model keeps full authority — it sees the
  partials + correction in one turn and re-plans the WHOLE batch (re-slicing
  tasks, reassigning goals — things steer cannot do).
- **(−)** Destroys work by design. Kills every child's process → each child's
  prompt/KV cache is gone (each child is its own `omp --mode rpc` process,
  `omp_rpc_transport.py:316-331`); re-dispatch re-pays full child tokens.
- **(−)** Graceful abort makes "stop" asynchronous (boundary-dependent), so
  the re-entry turn may start BEFORE the partials land → hermes re-plans
  against incomplete information, then a second completion event arrives
  (double-delivery of state, R3).
- **(−)** The #30170 demotion logic exists precisely because interrupting on
  every follow-up wastes subagent work (§1.3-i); this design must be
  explicitly requested, never implied by a plain message.

Touch points (estimate: ~80-150 LOC + tests):
`omp_delegation.py` (batch-scoped `stop_batch(delegation_id, graceful=True)`
~30 LOC next to `handle_omp_control_action`); `gateway/run.py` or
`slash_commands.py` (verb handler ~40 LOC); tests following
`tests/tools/test_omp_delegation.py` (fake transports at :688+) and
`tests/gateway/test_tui_gateway_queue_on_busy.py` patterns.

Verdict: ship as the KILL/REPLAN escape hatch, not as steering.

### Design B — side-channel direct steer (recommended core)

Give the user a gateway verb that forwards text straight into the M0A control
plane, bypassing the model round-trip. Two entry flavors:

**B1. Slash verbs** (deterministic, no parsing ambiguity):

- `/dsteer <child> <text>` — child = `subagent_id`, child `name`, or `all`
  (whole batch). `/dstop <child>` — graceful stop. `/dlist` — alias of the
  control list.
- Registry: `CommandDef("dsteer", ..., busy_policy="dispatch", busy_handler="dsteer")`
  in `mercury_cli/commands.py` (~3 entries); idle handler in
  `gateway/slash_commands.py` (`_handle_dsteer_command` → calls
  `handle_omp_control_action`); busy variant in the Guard-2 special table
  (`run.py:17810-17822`) — identical body, since the control plane is
  turn-independent.
- Ownership: `handle_omp_control_action` must receive a `parent_agent`-shaped
  object carrying the CHAT's durable `session_id` so `_owns_live_child`
  (:198-210) keeps refusing cross-conversation steers. The gateway has it
  (`session_entry.session_id`, same stamping as `omp_delegation.py:1099,
  1143-1152`). Without this the gate degrades to permissive.
- Name resolution: extend `_resolve_live_child` (:170-195) to match on
  `name` among records owned by this session (~15 LOC). The dispatch result
  already advertises names (`omp_delegation.py:1194-1202`).
- Live-batch detection for good errors: `async_delegation.has_live_for_session(
  session_key=...)` (`async_delegation.py:686-708`) already answers "does this
  chat own a running batch".

**B2. Plain-message `@name:` convention** (zero-friction, mirrors D12's
mention gating): when a session owns a live batch and an inbound TEXT starts
with `@<childname>:` (or `@batch:`), the intake steers directly and posts the
same style of ack the busy-steer path uses (`run.py:11193-11248`); no match →
untouched normal turn. Needs the same detector + name map; lives in the
busy/idle intake (~40 LOC).

Trade-offs:

- **(+)** Zero latency, zero tokens, zero model-compliance risk; children keep
  running → no cache invalidation, no child restart, no lost work. omp's steer
  semantics are already boundary-safe (§1.2).
- **(+)** Works in both windows (§2 precedent); the same handler serves
  Telegram/WhatsApp/Discord/CLI/TUI because it sits in the shared gateway
  mixin + registry.
- **(−)** The parent model does not automatically see the steer (the text
  goes to the child, not into the conversation). Mitigation (small, required):
  append the steer to the batch record (`steers: [{child, text, at}]`, ~15 LOC
  in `dispatch_async_delegation_batch`'s record + a helper) and include it in
  the consolidated completion header (`_push_batch_completion_event`,
  `async_delegation.py:1171-1236`) + post an ephemeral chat ack. Then hermes
  learns the course correction exactly when results arrive — one source of
  truth, no queued duplicate user turn (avoids R2).
- **(−)** Authorization: today model-facing steer is gated by ownership, and
  the gateway slash path is gated by `_is_user_authorized` / `slash_access`
  (`gateway/slash_access.py`). D7's rule "write access == steer authority"
  maps cleanly: any user allowed to message the chat may steer its children.
  `@name:` interception should be opt-in per platform (config flag) since it
  changes plain-text semantics.
- **(−)** Append-task (`+ new work for the wave`) does NOT fit B: the batch
  runner is fixed at dispatch. Appending = dispatch a SECOND batch while the
  first runs (already supported — one async slot each,
  `async_delegation.py:1092-1107`; completions coalesce per session,
  `run.py:27669-27700`). Document `/dsteer` as steer-only; appending is "send
  another delegate_task next turn" or a follow-on batch — acceptable.

Touch points (estimate: ~120-200 LOC + tests):
`omp_delegation.py` (name resolution + optional `steer_batch` ~25 LOC),
`async_delegation.py` (steer ledger + completion-header echo ~20 LOC),
`mercury_cli/commands.py` (registry ~6 LOC), `gateway/slash_commands.py`
(idle handler ~40 LOC), `gateway/run.py` (busy_handler entry ~10 LOC),
optional B2 intake hook (~40 LOC). Tests: extend
`tests/tools/test_omp_delegation.py` (registry/steer fakes already at :688+)
and add a gateway dispatch test asserting busy+idle both reach the control
plane with the session-id ownership stub.

### Design C — reuse the observatory ControlRouter as the gateway's steering front-end

Route gateway steering intents through `observatory/control.py`'s
`ControlRouter` (parse → gate → class-routed actions) instead of growing a
gateway-side parser; the gateway executes the returned actions against its
own in-process handles.

Assessment: the router's VALUE for this use case is its honesty ledger
(queued/applied acks, steer cap, stop boundary-waits — `control.py:86-96,
370-414`) and its intent vocabulary, but its SHAPE is Matrix-native: node ids
from the discovery tree, power-level providers, virtual-user senders, room
scoping (`control.py:215-250, 431+`). Adapting it to chat-session keys means
a parallel adapter layer of roughly the size of design B's handler — plus a
new shared dependency from the gateway hot path into the observatory package
(today the dependency points the other way: the observatory adopts gateway
handles, `sidecar_main.py:517-535`, `platform_hook.py:18-20`).

What C SHOULD contribute instead (borrow, don't wire): the queued/applied ack
pattern and the unconfirmed-steer cap for B's UX, and — when M4a/M5 lands its
engine transports — a shared "steer executor" module if both surfaces end up
formatting the same RPC calls. The M4a/M5 wiring will steer these same
children from Matrix rooms using the same `_live_children` handles; B's
`handle_omp_control_action` call is the correct single choke point both
surfaces should target (`sidecar_main.py:43-45` confirms transports are still
unwired there, so no duplication exists yet and none is needed).

Verdict: do not build C now. Build B thin; borrow C's ledger semantics.

## 4. Risks (cross-cutting)

- **R1 Prompt-cache**: steer (B) — none invalidated anywhere (children keep
  running; injection is a boundary user-message). Interrupt (A) — every killed
  child loses its process and cache; re-dispatch re-pays full tokens. Parent
  (hermes) context grows append-only in both (completion re-entries + steers
  ledger), so parent cache survives.
- **R2 Double-delivery**: a side-channel steer must NOT also be queued as a
  user turn (the busy-steer path already models this: "must NOT also be
  replayed", `run.py:11221-11237`). Record-and-echo-on-completion (B) is the
  single-delivery design. For B2, a failed name match must fall through
  UNTOUCHED or the user's normal message is eaten.
- **R3 Partial-result semantics (A only)**: graceful abort is
  boundary-asynchronous; the killed batch's completion event can arrive after
  the user's correction turn started. The re-dispatched batch then delivers a
  SECOND completion. Mitigation if A ships: after `stop_batch`, have the
  handler WAIT for the batch record to leave `running` (bounded, e.g. reuse
  `_STALL_GRACE_SECONDS`) before replying, so the correction turn and the
  partials land in order.
- **R4 Child restart**: none in B (children never restart); in A a re-dispatch
  creates a NEW `delegation_id` (ids never collide, `omp_delegation.py:1098`),
  but NAME reuse across waves means name resolution must scope to live
  children of the owning session only (already the ownership filter).
- **R5 One-shot fallback children** are kill-only (`steerable: false`): both
  designs must surface the existing honest error
  (`omp_delegation.py:290-295`) rather than pretending to steer.
- **R6 Authorization**: model-facing steer is ownership-gated; B adds a
  user-facing entry — keep `_owns_live_child` honest by passing the chat's
  durable session id, and keep the verb inside the existing authorized-slash
  path (`slash_access.py`). B2's plain-text interception must be config-gated
  per platform.

## 5. Recommendation

Ship **B1 (slash verbs) + the steer ledger/echo**, borrow C's queued/applied
ack copy, and land **A only as `/dstop-all`-style graceful batch stop** (the
stop half of A — no auto-requeue; hermes re-plans on the next turn when the
partials re-enter, which is the async model already in place). B2 (`@name:`
interception) is a config-gated follow-up once B1 proves the plumbing. Do not
build a second control router (C) and do not add Matrix-side anything — that
is M4a/M5's lane against the same handles (§2, observatory D12/D13 coverage).

Suggested slice order (each independently shippable):
1. `handle_omp_control_action` ownership stub from the gateway + name
   resolution in `_resolve_live_child` + `/dlist` verb (read-only, zero risk).
2. `/dsteer <child|all>` + `/dstop <child>` + steer ledger echoed in the
   consolidated completion header.
3. Graceful batch stop verb (`stop_batch`) reusing the same ownership gate.
4. (Optional, config-gated) B2 `@name:` intake interception with busy-steer
   style acks.

## 6. Fact appendix (key citations)

- Dispatch/handles: `hermes/tools/omp_delegation.py:87-167, 1006-1205`
- Control plane (steer/stop/list, ownership): `omp_delegation.py:170-345`
- RPC child transport (steer/abort/subagent_*): `hermes/tools/omp_rpc_transport.py:261-499`
- omp RPC steer server dispatch: `omp/packages/coding-agent/src/modes/rpc/rpc-mode.ts:1271-1273`
- Async batch registry, durable rows, interrupt, staleness:
  `hermes/tools/async_delegation.py:761-1236, 1541-1595` (staleness :94-121)
- Completion delivery: `hermes/gateway/run.py:14368, 27222-27460, 27646-27860`
- Busy-text intake (interrupt/queue/steer, #30170 demotion):
  `gateway/run.py:10881, 11019-11323` (oracle :10677)
- Guard-2 busy-slash dispatch: `gateway/run.py:17741-17842`; `/steer` busy
  handler :17961-18008; `/queue` :17920-17959
- CommandDef busy semantics: `hermes/mercury_cli/commands.py:99-140`
  (`/steer` :213-214, `/agents` :204-205, `/stop` :189-190)
- Idle `/stop` (batches untouched): `hermes/gateway/slash_commands.py:1314-1395`;
  reset interrupts batches :218-224; `/agents` delegation listing :1163-1312
- Model-facing delegate_task control schema: `hermes/tools/delegate_tool.py:5229-5254`
- Legacy steer (prior art, dead path for omp children):
  `delegate_tool.py:268-392, 2794-2795`; `tui_gateway/methods_session.py:3443-3486`
- omp collab live steering: `omp/packages/coding-agent/src/collab/protocol.ts:64-67`,
  `guest.ts:204-214`
- Observatory router + coverage: `hermes/observatory/control.py:59-134, 142-340`;
  `sidecar_main.py:19-63, 517-535`; `docs/design/matrix-observatory.md:31-48 (D5-D14), 142-201, 275-301`
