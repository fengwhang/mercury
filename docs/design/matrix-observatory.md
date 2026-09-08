# Matrix Observatory — design spec

Status: DESIGN AGREED (grilling session 2026-09-07, hermes+user). Not yet implemented.
Owner: phoenix. Truth doc for implementation; every section cites the facts it rests on.

## 0. What this is

A first-class Matrix UI for Mercury, bundled with the distribution: a local
homeserver + appservice sidecar that mirrors every live agent session on the
machine as a tree of nested Matrix spaces, streams their tool calls (and
omp-side reasoning) into per-agent encrypted rooms, and lets the owner steer,
stop, approve, and converse with any agent — orchestrator or subagent,
hermes-side or omp-side — from a phone running Element X.

Positioning: Matrix becomes the *primary* chat surface for Mercury (like
Telegram is for hermes/openclaw today, but local and out-of-the-box). The
existing upstream hermes matrix client-bot plugin is demoted and relabeled
**"Secondary chat"** (bring-your-own homeserver/account); it is not modified
beyond the label (facts: `hermes/plugins/platforms/matrix/adapter.py` is a
single-MXID client bot — join-only, zero spaces support, zero appservice code,
`create_room` exists but has no production caller).

Non-goals: federation (server is single-owner, LAN/localhost only); hermes
orchestrator chain-of-thought exposure (stays hidden as today); capping the
number of live orchestrators (explicitly rejected — no cap, ever).

## 1. Settled decisions (the contract)

| # | Decision |
|---|----------|
| D1 | Bundled in the Mercury distro as a service peer of the SimpleX integration; default ON at install; `observatory.enabled: false` freezes (never deletes). |
| D2 | Ships its own homeserver: Tuwunel (Apache-2.0, single static Rust binary, Synapse-admin API, Simplified Sliding Sync → Element X compatible, appservice support, no per-user rate limiter). Closed registration, federation off, bound to localhost/LAN; mobile reaches it over Tailscale/existing VPN. |
| D3 | Sidecar owns ALL matrix I/O as an **application service**; one **virtual user per agent** (`@<slug>:server`), spaces/subspaces encode the agent tree; existing client-bot plugin stays as "Secondary chat", sharing a lifted matrix-common library (E2EE machine, markdown sanitizer) — no overlap in homeserver ownership. |
| D4 | E2EE on **every** room the sidecar creates (gateway room, orchestrator rooms, every subagent room). mautrix OlmMachine per virtual user, bridge-pattern key bootstrap. |
| D5 | Full steering: chat with any delegate_task child AND any omp in-process subagent (grandchildren), all depths. One message per tool call; omp-side thinking streamed (separate quoted messages); hermes-orchestrator CoT never shown. |
| D6 | Agent `name` is hard-required at both spawn surfaces (hermes `delegate_task`, omp Task tool). Unicode allowed in display/room names; MXID localparts are strict-lowercase-ASCII slugs (Tuwunel grammar: `a-z 0-9 . _ = / +`). |
| D7 | Single owner + invited users; permission = Matrix power levels; **write access to an agent's room == authority to steer that agent**. |
| D8 | Annihilation semantics ("die" = task complete for subagents; `/exit` for spawned orchestrators; session end/reset for gateway conversations). Depth indexing: 0-agents = top-level orchestrators (spawned hermes/omp) AND the gateway agent; 1-agents = their direct children (incl. cron-fired agent children); ≥2-agents = deeper. Deletion timing BY DEPTH: a **1-agent's room/space is destroyed the instant it dies** (final summary posts to the PARENT's room only — its own room is being purged); a **≥2-agent's room/space survives until its parent dies** (parent lifetime = reading grace, no timer); a **0-agent's history survives until it dies** (`/exit` or session reset). Parent death cascades: children still alive at parent death are stopped first (existing cascade: batch interrupt/SIGKILL process group, omp abort signal), then the whole subtree is server-side DELETED (Tuwunel admin `DELETE /_synapse/admin/v1/rooms/{id}` — true PDU purge, not kick+redact). History persists only in local state.db / session JSONL. |
| D17 | Virtual accounts (MXIDs) are never deactivated. A new agent whose name matches an inert predecessor inherits the MXID — and NOTHING else: fresh agent session, freshly created room/space (the old ones were deleted per D8), zero context/state inheritance. Display-name collision suffixes count *live* agents only. |
| D18 | **0-agent resilience**: gateway restart, mercury update, sidecar restart, or homeserver restart NEVER deletes 0-agent spaces/rooms — deletion happens only via the `/exit` cascade (D8); restart is not death. On gateway start the sidecar runs a **respawn pass** (before serving traffic): for every live 0-agent in observatory state.db, resume its session — hermes-side: resume the state.db session; omp-side: restart the headless RPC process attached to its existing session JSONL — reattach the SAME rooms/spaces (they persist in Tuwunel's RocksDB; the sidecar re-ensures membership + tree), same MXIDs (D17). Subagents get NO respawn: orphaned children are finalized by the existing async_delegations restart-recovery (stale monitor / force-finalize), and their summaries are delivered to the respawned parent. Applies to both /spawn and /spawnomp orchestrators; the gateway agent itself is trivially resilient (it IS the gateway process; its room/space are ordinary persistent rooms). |
| D9 | `/spawn <name>` creates a hermes-side orchestrator (name-only, no goal; works exactly like the current CLI chat: indefinite lifespan, resumable; only `/exit` ends it — process kill + space annihilation, transcript stays local). `/spawnomp <name>` same for an omp-side orchestrator (headless `--mode rpc`, like `mercury omp` in bash). No cap on live orchestrators (dashboard shows the count). |
| D10 | Approvals: `/approve`, `/deny` (and `!`-prefix) as replies to the approval prompt — no reaction-based approvals (explicitly rejected). |
| D11 | Cron: one room per *job* (not per fire) directly in the gateway space; per-fire rolling content. |
| D12 | Directives room in the gateway space (see §6): membership = the gateway agent + every live 0-agent (both /spawn and /spawnomp kinds, auto-maintained by the sidecar); delivery is mention-gated — a directive reaches ONLY the agents whose virtual user is @-mentioned; @room/@everyone = all members; replies go to each agent's own room, never the directives room. |
| D13 | Commands: sidecar verbs recognize both `/verb` and `!verb` (Matrix clients reserve `/`; Element passes unknown `/words` through as text). Everything else starting with `/` routes to the agent's native command surface: hermes rooms → gateway slash dispatch (full registry), omp rooms → omp RPC `prompt` 3-stage handling (ACP builtins intercept; unmatched text reaches the model). Command SCOPE by agent class: gateway-lifecycle verbs whose blast radius is the gateway process itself (`/restart`, `/update`, pause/resume-for-update) are accepted ONLY from the gateway agent's room — spawned 0-agents and all deeper agents get session-scoped commands for their own session only (e.g. `/reset`, `/compact`, model pickers), never process-level ones. The gateway agent is a 0-agent for deletion semantics (D8); the difference is exactly this command scope. |
| D14 | Discovery covers every active agent session on the machine: all delegate_task children (any origin: gateway, CLI, cron), all omp in-process subagents, spawned orchestrators, and manual terminal `omp` runs — the last observe-only in v1 (no RPC server exists in TUI mode; collab relay is not self-hostable in production). |
| D15 | Element X is the reference client; nested subspaces verified rendering on iOS+Android (element-meta#2913 closed, flags removed 2026-01). A rolling-edited dashboard message remains as insurance + "what's running" summary. |
| D16 | Tuwunel is fetched at install time (latest stable release from upstream releases) — NOT vendored/packed like omp — and `mercury update` (updater: `mercury_cli/cli_commands_mixin.py:4019`) refreshes it to latest stable + restarts `mercury-observatory-homeserver.service`. Mercury never modifies Tuwunel; all interaction is external interfaces (appservice registration file, Synapse-compatible admin API, client-server API). |

## 2. Architecture

Components (process model mirrors simplex-chat.service precedent):

1. **Tuwunel** — systemd user unit `mercury-observatory-homeserver.service`.
   Fetched at install time from the LATEST STABLE upstream release (D16 —
   not vendored; `mercury update` refreshes it); pinned minimum ≥1.8.1
   (Synapse admin API introduction), config
   generated: `allow_federation = false`, `allow_registration = false`
   (+ `registration_token` for the owner's own onboarding), appservice
   registration written at provision time.
2. **Sidecar daemon** — `hermes/observatory/` (new package), systemd user
   unit `mercury-observatory.service`, Python/asyncio in the gateway venv.
   Subsystems:
   - **Appservice endpoint** (mautrix appservice framework): transaction
     intake, virtual-user masquerade via `?user_id=`, namespace
     `@merc_.*` (exclusive, anchored).
   - **Discovery**: subscribes to hermes delegation lifecycle (see §7) +
     scans omp session dirs for manual runs; maintains the agent tree in
     SQLite (`<MERCURY_HOME>/observatory/state.db`): node id, parent node,
     engine (hermes|omp), session file / delegation id, virtual MXID, space
     id, room id, status.
   - **RPC fan-out**: one persistent connection per live omp child process
     (`omp --mode rpc`), `set_subagent_subscription("events")`; for
     hermes-side sessions, gateway WS JSON-RPC client.
   - **Renderer**: tree → spaces (`m.space.child` state), events → room
     messages (one per tool call; thinking as separate messages; rolling
     dashboard edit).
   - **Control router**: room messages → steer/abort/commands/approvals.
3. **Gateway integration** — the observatory registers as a platform
   plugin (pattern: `plugins/platforms/simplex/adapter.py` register(ctx),
   env-driven enablement) for matrix-born orchestrator conversations; the
   sidecar remains the sole matrix client. Non-matrix sessions (SimpleX,
   CLI, cron) are mirrored, never duplicated as chat (D-decision: overlay
   observe/steer for foreign sessions; native chat only for matrix-born).

## 3. Space tree layout

```
Gateway space  (root; name "Mercury — <host>")
├─ Gateway agent subspace    (the gateway agent is a FULL 0-agent — parity
│    with /spawn: its own space, created at boot, never repurposed)
│   ├─ Gateway agent room    (the simplex-chat equivalent: default convo + cron notifications)
│   └─ delegation subspaces  (gateway-origin delegate_task children — same
│        nesting law as any orchestrator's children, any depth)
├─ Directives room           (§6; owner→all top-level hermes orchestrators)
├─ Cron space?  NO — cron rooms sit directly in the gateway space (D11)
│   ├─ cron:<job-name>       (one per cron job, rolling per-fire history)
├─ Orchestrator subspace "<name>"   (one per /spawn or /spawnomp — created at spawn, before any delegation)
│   ├─ <name> room           (its conversation; for spawnomp, the main omp session)
│   ├─ subagent rooms        (delegate_task children — for hermes orchestrators)
│   │   └─ …nested subspaces for grandchildren (omp internal subagents), any depth
│   └─ dashboard message     (rolling edited status: live tree, N agents, M delegations, K blocked)
└─ (manual omp runs)         observed sessions appear under a "Manual runs" subspace, read-only
```

Rules:
- Every agent = one space (even before it has children) + one chat room
  inside it — the gateway agent included (gw-space parity): its subspace
  holds its room and nests its gateway-origin delegation children.
  Nesting = `m.space.child` in the parent space. No thread-based
  nesting (threads are flat by spec, MSC3440; and labs-WIP on Element X).
- Child ordering in the gateway space: gateway agent subspace first,
  then directives room, cron rooms, orchestrator subspaces, manual runs.
- Node disappears from the live tree when the agent settles; its space is
  deleted per D8. Settled-but-not-yet-deleted rooms get a "settled —
  transcript only" state; steering disabled.
- Isolated omp subagents are in-process sessions (worktree cwd, not
  subprocesses — verified: no child_process in omp/task/): they appear and
  steer like any grandchild, but revive is impossible by design (no
  reviver registered) — the room offers kill/steer only while live.

## 4. Identity, naming, permissions

- Virtual user per agent: MXID `@<slug>` where slug = strict grammar
  (`^[a-z0-9][a-z0-9._=/+-]*$`), derived from the agent's name
  (unicode → transliterate/slug; untransliteratable → `agent-<hash8>`),
  collision suffix `-2`, `-3` among *live* agents. Display name =
  user-chosen unicode name + tree position ("2.1 auth-refactor"). Room
  names/space names: full unicode.
- Appservice registration: exclusive user namespace `@merc_.*` — wait:
  slugs are user-provided; namespace covers ALL virtual users, so
  namespace regex must be the reserved prefix form. RESOLVED: all
  observatory virtual users carry prefix `merc_` (`@merc_<slug>`); the
  prefix is stripped in display names. (Anchored regex, exclusive.)
- Permissions: owner = PL 100 everywhere. Invited users: per-room
  `m.room.power_levels` set by the sidecar at invite time (admin chooses
  read-only = `events_default` above their level, or write). Write in an
  agent room = steer authority for that agent (server-enforced sending +
  sidecar enforces steer-only-while-running). Read-only users see
  everything (including omp thinking) but their messages in agent rooms
  are rejected by the sidecar with an explanatory notice.
- Onboarding: first gateway start provisions homeserver + owner account +
  appservice; prints/DMs a setup card (homeserver URL + owner
  credentials). Element X: manual homeserver URL entry (QR transfer only
  works from an authenticated Element Web session — not a self-hosted
  login path).

## 5. Event flow (per agent)

Streaming into a room:
1. **Tool calls**: one message per call — tool name + args truncated
   ~200 chars + `[full]` marker; results elided except errors. Full text
   lives in local transcripts only (state.db for hermes, session JSONL
   for omp).
2. **Thinking (omp-side only)**: separate quoted/italic messages between
   tool-call messages; toggle per room `/cot on|off` (default on).
   Source: `subagent_event` frames carrying `thinking_delta` /
   `message_end` (already in the RPC stream at subscription level
   "events"; mercury's TUI hides thinking at display layer only).
3. **Lifecycle**: created → running → dead. Final summary posting and
   deletion follow D8's depth rule: 1-agents — summary to PARENT's room
   only, room/space purged instantly; ≥2-agents — summary to its room
   AND parent's room, room/space survives until parent dies (parent
   lifetime = reading grace, no timer); 0-agents — history survives
   until /exit / session reset. Dead-but-not-yet-deleted rooms show
   "settled — transcript only"; steering disabled.
4. **Dashboard**: one rolling edited message per orchestrator space +
   the root dashboard in the gateway room (edits are silent —
   `m.rule.suppress_edits`).

Steering (control router):
- Plain message in a live agent's room (write PL required):
  - hermes-side child or orchestrator → injected as user message at the
    next iteration boundary (gateway `slash.exec`/session injection path).
  - omp-side (main or subagent) → RPC `steer` / new `subagent_steer`
    (§8). Room shows "⏳ queued steer" then "✔ applied" when the
    injection fires; `/stop` shows "🛑 stop requested — waiting for
    boundary" until confirmed kill.
- Approvals: hermes guard approval requests surface in the requesting
  agent's room as a prompt; resolution by `/approve` / `/deny` reply
  (option words `once|session|always` accepted as `/approve always`).
  Routing reuses the existing approval plumbing
  (`tools.approval.resolve_gateway_approval`, approval socket
  `$TMPDIR/mercury-approval-*/approval.sock`).

## 6. Directives room

One room in the gateway (root) space, directly below the gateway agent's
subspace, above cron rooms. Purpose: owner → selected top-level agents.

Membership: the gateway agent + every live 0-agent (both /spawn and
/spawnomp kinds) — auto-maintained by the sidecar (agents join at
spawn/respawn, leave at /exit death; the D18 respawn pass re-ensures
membership).

Mechanics — mention-gated delivery: the owner's message reaches ONLY
the agents whose virtual user is @-mentioned in it. @room/@everyone =
all members (the supertool). Arbitrary subsets by @-ing combinations.
No valid member mention → no delivery; the sidecar posts a short help
notice naming the current members. Delivery per engine: hermes-side →
inject as labeled user message ("[directive] <text>") into the session
(new turn if idle; queued after the current turn if busy); omp-side →
RPC `steer` on the orchestrator's main session (same mid-run boundary
semantics as any steer; if idle, prompt a new turn). Agents NEVER
respond in this room — replies land in their own rooms. A rolling
edited message tracks delivery per agent ("✔ applied: auth-refactor
14:02 · docs-sweep (queued)"). Write = owner PL only.

## 7. Discovery & data sources (facts → wiring)

| Source | Fact (verified 2026-09-07) | Wiring |
|---|---|---|
| hermes delegations | `async_delegations` table in `<HERMES_HOME>/state.db` (deleg_id, parent session, state running/completed/…); hermes in-process `_active_subagents` registry bypassed in Mercury | Sidecar polls table + subscribes to plugin hooks `subagent_start`/`subagent_stop` (`mercury_cli/plugins.py:226`, fired `delegate_tool.py:2199`) for instant events |
| omp children (delegate_task) | one `omp --mode rpc` process per task; `/proc/<pid>/environ` carries `HERMES_SESSION_ID`; RPC protocol: `get_subagents`, `get_subagent_messages`, `set_subagent_subscription`, frames `subagent_lifecycle/progress/event` | Sidecar connects RPC per child (transport: `tools/omp_rpc_transport.py` client, extended with subagent methods — the vendored python client has none yet) |
| omp internals | AgentRegistry (`AgentRef {id, displayName, kind, parentId, status, sessionFile…}`), tombstone files on kill, isolated children are in-process (worktree cwd) | Registry snapshot via `get_subagents` + lifecycle frames; nesting via `parentToolCallId`/registry parentId |
| manual omp runs | sessions `<agentDir>/sessions/--<cwd>--/<ts>_<uuid>.jsonl` + artifact dirs with subagent JSONLs; NO external control channel in TUI mode; collab relay closed-source (`docs/collab.md:113-117`), dev stand-in `local-relay.ts` exists | v1: scan + tail JSONLs, observe-only under "Manual runs" subspace. BLOCKER FIX REQUIRED: gateway systemd unit must export `PI_CODING_AGENT_DIR=$MERCURY_HOME/omp` (bin/mercury:76 does; `generate_systemd_unit` at `mercury_cli/gateway.py:3995` does not — today gateway-spawned children would write `~/.omp/agent`), else two session roots exist |
| transcripts | hermes: state.db `messages` (tool_calls JSON, reasoning persisted); omp: JSONL with toolCall blocks (full args), toolResult, thinking blocks in plaintext | Renderer reads via RPC/DB, never re-parses transcripts for live flow |

## 8. Engine patches (both marked `HERMES-OMP PATCH`, re-pin discipline)

### 8.1 hermes side
1. `tools/delegate_tool.py` — `DELEGATE_TASK_SCHEMA.tasks.items`: add
   `name` (string, hard-required) → `required: ["goal", "name"]`
   (schema at :5104-5148); handler derives fallback names for
   non-model callers (cron/legacy: `task-<n>`) so nothing breaks, but
   the model-facing schema + dynamic description text mandate it.
2. `tools/omp_delegation.py` — stop rejecting `action='steer'|'stop'`
   (:726-750); forward to the child's RPC connection (`steer`,
   `abort`), thread through `delegate_task` control actions.
3. `~/.mercury/config/HERMES.md` (+ repo-side template) — document the
   delegation parameters incl. mandatory short `name` and naming
   guidance (task-relevant, lowercase-kebab *recommended* but unicode
   accepted; this is the model-facing doc the orchestrator reads).
4. Gateway unit generation: export `PI_CODING_AGENT_DIR` (see §7).
5. `observatory` plugin registration (platform plugin pattern) +
   `/spawn`/`/spawnomp` session factory (fresh hermes session / headless
   omp rpc session, matrix room = its conversation).

### 8.2 omp side (vendored fork, patches marked in-source)
1. `packages/coding-agent/src/task/types.ts` — `"name?"` → `"name"` at
   :115, :123, :151, :160 (taskItemSchema, taskItemSchemaIsolated,
   taskSchema, taskSchemaNoIsolation; batch forms inherit). TS types
   stay optional defensively. Update `docs/tools/task.md`.
2. RPC subagent control — new commands in
   `packages/coding-agent/src/modes/rpc/rpc-types.ts` (`RpcCommand`
   :28-93): `subagent_steer {subagentId, text}` and
   `subagent_abort {subagentId, reason}` + response variants; handlers
   in `rpc-mode.ts` switch calling the EXISTING in-process machinery:
   `AgentLifecycleManager.global().ensureLive(id)`
   (`src/registry/agent-lifecycle.ts:317`) +
   `session.prompt(text, {streamingBehavior: "steer"})` /
   `ref.session.abort({reason})` + `lifecycle.release(id, ref,
   {tombstone:true})` — the exact paths proven by collab host
   (`src/collab/host.ts:593-637`) and TUI Agent Hub
   (`src/modes/components/agent-hub.ts:1530-1535`). Isolated children:
   steer/kill work mid-run; revive impossible (by design). Mark both
   patches `HERMES-OMP PATCH` for re-pin.
3. Python RPC client (`omp/python/omp-rpc/` or hermes
   `tools/omp_rpc_transport.py`): add `get_subagents`,
   `set_subagent_subscription`, `get_subagent_messages`, `steer`,
   `abort`, `subagent_steer`, `subagent_abort`.

## 9. Failure modes

- Sidecar dies → rooms freeze (no updates), nothing deleted; on restart,
  rebuild tree from state.db + `get_subagents` snapshots + `since`-byte
  markers per transcript; reconnect replays missed events. 0-agent
  spaces/rooms are restart-immune (D18) and their sessions are resumed
  by the respawn pass before the sidecar serves traffic again.
- Homeserver dies → systemd restarts unit; sidecar re-registers
  (appservice registration is file-based, idempotent).
- `observatory.enabled: false` → sidecar stops updating; rooms preserved
  frozen; re-enable resumes.
- Agent dies while user is mid-scroll → D8's depth rule: 1-agent rooms
  vanish instantly (summary landed in the parent's room); ≥2-agent
  rooms stay until the parent dies — no timer races.
- Ostensible risk accepted: no rate limiting exists on Tuwunel per-user
  (verified) — the appservice will not be throttled; do not expose the
  homeserver beyond the VPN.

## 10. Implementation order

Phase 0 — prerequisites (independent value, ship first):
  PI_CODING_AGENT_DIR gateway fix; delegate_task `name` (schema +
  handler fallback); omp `name` hard-required; HERMES.md delegation docs.
  Tests: schema validation, fallback naming, one LIVE fan-out with names.
Phase 1 — omp RPC control: `subagent_steer`/`subagent_abort` + python
  client methods; hermes-side steer/stop forwarding. Tests: fake-server
  E2E (pattern of tests/tools/test_omp_rpc_transport.py) + LIVE steer of
  a running child.
Phase 2 — Tuwunel bundling: install.sh fetch + pinned version, config
  generation, provisioning script (owner account, registration closed,
  appservice registration), systemd units, "Secondary chat" relabel.
Phase 3 — sidecar core: appservice endpoint, discovery (hooks + state.db
  poll + RPC connect), space/room provisioning, streaming renderer
  (tool calls, omp thinking, lifecycle), dashboard, virtual users, E2EE
  per room. LIVE gate: watch a real 3-deep fan-out render on Element X.
Phase 4 — control router: steering UX (queued/applied states), `/stop`,
  approvals via `/approve`/`/deny`, command routing (gateway slash.exec
  for hermes rooms; omp prompt pass-through).
Phase 5 — orchestrators: `/spawn`, `/spawnomp`, `/exit`, directives
  room, cron job rooms, "Manual runs" observe-only discovery, and the
  D18 respawn pass on gateway/sidecar start (resume live 0-agents,
  reattach same rooms/spaces/MXIDs; no subagent respawn — orphaned
  children go through async_delegations restart-recovery).
Phase 6 — polish: onboarding card, dashboard summary line, freeze/replay
  hardening, docs (website + README section).

Per repo law: every push ships a new 0.0.N release; LIVE verification
markers in TODO.md for each phase gate.

## 11. Open items (non-blocking, tracked)

- O1: collab relay self-hosting — if upstream ever publishes the relay,
  full-fidelity steering of manual TUI runs becomes possible (v2).
- O2: Element X widget support — a richer spawn UI than the command room
  if/when EX supports widgets.
- O3: E2EE device verification UX for N virtual users — the bridge
  pattern (bot device verified once, virtual users chain) needs a proof
  at Phase 3; if verification friction is high, fall back to
  owner-verifies-first-virtual-user + trust-on-first-use for the rest.
- O4: hermes-orchestrator thinking panes — currently hidden; revisit if
  the user ever wants orchestrator CoT (today: explicitly no).
- O5: name sanitizer details — unicode display names need a length cap
  and bidi/zwj sanity rules at render time (room names), decided at
  Phase 0 implementation.

Pre-release status (2026-09-08, final-sweep):
- E2EE gate: FAIL — `~/.mercury/observatory-build/gate-e2ee.log` reports
  MERCURY-E2EE-FAIL (M_UNKNOWN_TOKEN on login, 1/1 checks); O3's Phase-3
  proof still open, owned by the e2ee-gate2 agent. Blocks release.
- Remote reachability: D2 stands — homeserver bound to localhost/LAN,
  mobile over the owner's Tailscale/existing VPN; no dedicated VPN
  shipped, no further decision taken.
- User→running-batch steering UX: see
  `docs/design/midflight-steering.md` (investigation, no code changes) —
  the observatory room verbs (§5) are the matrix-surface counterpart of
  the chat-surface wiring it proposes.

## 12. Fact appendix (key citations)

- hermes delegate_task schema/handler: `hermes/tools/delegate_tool.py:5081-5185`
- omp rejection of steer/stop today: `hermes/tools/omp_delegation.py:726-750`
- omp spawn/kill: `hermes/tools/omp_delegation.py:87-131, 723-900`; transport `hermes/tools/omp_rpc_transport.py:270-291`
- omp Task schemas (`name?` sites): `omp/packages/coding-agent/src/task/types.ts:114-166`
- omp name→agentId: `omp/packages/coding-agent/src/task/index.ts:807`
- RPC commands/frames: `omp/.../modes/rpc/rpc-types.ts:28-93, 348-365`; dispatch `rpc-mode.ts:1093-1296` (prompt 3-stage :1093-1155; steer :1157; abort :1167; subagent getters :1259-1296)
- Subagent event streaming incl. thinking: `omp/.../task/executor.ts:1444-1447, 1779-1780, 2839-2929, 3358-3379`; `rpc-subagents.ts:210-246`; `packages/ai/src/types.ts:1333-1336`
- Steer machinery: `src/registry/agent-lifecycle.ts:317, 388-391`; `src/collab/host.ts:593-637`; `src/modes/components/agent-hub.ts:1530-1535`; `src/tools/hub/messaging.ts:228-316`
- Isolation is in-process, default off: `task/isolation-runner.ts:197-309`; `config/settings-schema.ts:4905-4907`; `task/structured-subagent.ts:297-303`
- Gateway slash dispatch + external surfaces: `mercury_cli/commands.py:147`; `gateway/run.py:18855-18988`; WS JSON-RPC `slash.exec` `tui_gateway/methods_tools.py:1166`, `command.dispatch` :469; control socket verbs `gateway/control_socket.py:251-383`
- Approvals plumbing: `tools.approval.resolve_gateway_approval` (matrix adapter :4043-4080 pattern); approval socket `omp_delegation.py:347-458`
- Existing matrix plugin (Secondary chat): `hermes/plugins/platforms/matrix/adapter.py` — E2EE :1885-1963, create_room unused :4403-4441, no spaces/appservice (repo-wide), plugin.yaml registration :5444-5464
- Tuwunel: admin API compliance `docs/development/compliance/synapse-admin.md`; delete_room `src/service/rooms/delete/mod.rs:71-236`; appservice masquerade `src/api/router/auth/appservice.rs:7-23`; strict localparts `src/api/client/register/register.rs:178-263` (ruma `user_id.rs:41-62`); no per-user limiter (override_ratelimit 🟥)
- Element X: nested spaces element-meta#2913 (+ios#4485, +android#5297, closed 2025-10-08); flags removed Jan 2026; Spaces tab flat-top/drill-down; threads flat (MSC3440) and labs-WIP; edits silent (`m.rule.suppress_edits`); QR login only scans-from-Element-Web (#1915)
- collab relay not self-hostable: `omp/docs/collab.md:113-117`; dev stand-in `packages/collab-web/scripts/local-relay.ts`
- HERMES.md (model-facing delegation docs target): `~/.mercury/config/HERMES.md`, loaded via `agent/prompt_builder.py` / `mercury_constants.py`
