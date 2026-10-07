# Internal agent hub

Audit pins: Mercury `de312dad74dc198688fefba8434eab90a9b49bb3` (vendored OMP tree `c1aa0781d2d912e63436bb365270fe09a166eb2f`; declared upstream pin `d2d2c17368c5078c33f502476876c77574400675`, v18.1.6), upstream OMP `2f9d6d6b2494c89d422a18c2899f715f41a2ad67` (v18.8.2). Upstream cached reference `6d8552d7f9df1852826923f07f0eed4fe29511f3` (v18.6.0). Table line numbers refer to the audited base, not post-change line numbers.

## Audited rules

| Concern | Actual native rule | Source |
|---|---|---|
| Transport | Process-global mailbox bus; **not network IRC**, no IRC server/channel/JOIN/PART | `omp/packages/coding-agent/src/mirc/bus.ts:1-16,66-93`; upstream `src/irc/bus.ts:1-54` |
| Activation | Every subagent; root if task recursion capacity exists (default max depth 2). Explicit session `enableMirc:false` / restricted invocation excludes messaging. No `irc.enabled` setting | `src/tools/hub/messaging.ts:103-115`; `src/sdk.ts:1815`; upstream `src/irc/messaging.ts:16-21` |
| Membership | All alive non-advisor agents in one process/conversation, except caller. **Flat**, not sibling-only: root, parent, grandparents, nested descendants and cousins included | `src/registry/agent-registry.ts:273-282` |
| Parent | Main participates; peers can DM it; sibling traffic also has display-only root UI relay | `src/mirc/bus.ts:473-499` |
| Parent's parent channel | No channels exist. All levels use the same registry; independent roots/processes are isolated | Same registry and bus sources |
| Identity | Root `Main`, children registry IDs/task names; parentId is metadata, not membership ACL | `src/registry/agent-registry.ts:16,72-87` |
| Broadcast | `send to:all` expands all running/idle peers, not parked peers; no broadcast await | `src/tools/hub/messaging.ts:245-252,298-315` |
| Direct send | Immediate delivery receipt: injected, woken, revived, failed. Busy peer aside, parent message steering, idle wake, parked lifecycle revival | `src/mirc/bus.ts:95-238`; `src/session/mirc-bridge.ts:158-215` |
| Sequencing | Snowflake ID + timestamp. Oldest matching waiter wins. Successful delivery is not also buffered; failed live handoff buffers, cap 100 | `src/mirc/bus.ts:119-133,207-238,427-470` |
| Receive | Async session injection; `wait`, `inbox`, `list`. Wait supports from filter/abort/liveness; default timeout 120s, zero disables | `src/tools/hub/messaging.ts:122-150,396-453` |
| List/history | Default running+idle bounded; parked explicitly queryable in current root; retained transcript at history URI | `src/tools/hub/messaging.ts:63-81,152-218` |
| Approval | Messaging read-only, process mutation still exec-approved; incoming peer content escaped and agent-attributed, not owner/system | `src/tools/hub/index.ts:141-165`; `src/session/mirc-bridge.ts:169-194` |
| Completion | Ordinary native children retained idle, TTL park/revive; non-keepalive unregister; isolated terminal park; hard abort tombstone. Mercury Observatory depth-1 workers are deliberately terminal | `src/task/executor.ts:2623-2755,2980-2983` |
| Restart | Native process mailbox is not durable. Transcript/lifecycle restoration is separate. Never infer delivery from reconnect or registry presence | Bus + `src/registry/persisted-agents.ts` |

Upstream v18.8.2 no longer exposes the vendored model-facing `hub` messaging tool: sending is a read-tier `write` to `agent://<id>` or `agent://all` (`src/internal-urls/agent-protocol.ts:172-204`); the parameterless `wait` receives queued messages but only blocks while the caller owns a running job/service (`src/tools/wait.ts:24-25,43-47,87-120`). `/hub` is a **TUI activity overlay**, not IRC transport (`src/slash-commands/builtin-session.ts:583-589`). Message availability still derives from task depth/capacity, with write-tool availability additionally required for child URI messaging (`src/task/executor.ts:3967-3969`). `await/replyTo` sugar is absent upstream. Flat roster, local mailbox, async injection and parked revival remain (`src/registry/agent-registry.ts:324-331`). Mercury retains its vendored tool contract; this feature is not an upstream API/version upgrade.

Source links: [upstream bus](https://github.com/can1357/oh-my-pi/blob/2f9d6d6b2494c89d422a18c2899f715f41a2ad67/packages/coding-agent/src/irc/bus.ts), [upstream messaging](https://github.com/can1357/oh-my-pi/blob/2f9d6d6b2494c89d422a18c2899f715f41a2ad67/packages/coding-agent/src/irc/messaging.ts), [upstream registry](https://github.com/can1357/oh-my-pi/blob/2f9d6d6b2494c89d422a18c2899f715f41a2ad67/packages/coding-agent/src/registry/agent-registry.ts).

## Missing seam

Hermes `tools/omp_delegation.py:_run_omp_task` launches one process per child. Each creates its own `Main` and process-global registry, so identical hub tools cannot reach siblings or the Hermes parent. Observatory node IDs/rooms are presentation, not the missing native mailbox scope. The adapter must bridge the native bus and registry at the session capability boundary, with one local conversation scope and per-child identities, without changing Observatory feeds, room history, model/provider calls, or approval handling.

## Mercury activation

Use `mercury chat` for the Hermes engine or `mercury omp` for native OMP. Internal peer coordination follows the **existing delegation capability** and native `omp.task.maxRecursionDepth` setting (default 2). There is no IRC host, mLounge room, `irc.enabled` switch, or separate hub setup.

With delegation granted, the Hermes parent exposes a per-session `hub` capability: `list`, `send`, `wait`, `inbox`. Use `delegate_task` with named tasks; each running OMP child receives an explicit scope/identity and can use its existing native `hub` tool. The Hermes parent is `Main`; child addresses match the returned delegation child handles (`delegation_id/index`). Use actual roster IDs, not guessed names. `send` with `to:"all"` includes live root/parents/descendants/cousins in **this conversation**, not other conversations/profiles. Two batches in the same conversation share that native flat namespace.

Setting `omp.task.maxRecursionDepth: 0` removes root peer capability. Removing `delegate_task` from a Hermes agent's allowed toolset likewise removes its internal hub. SDK `enableMirc:false` and restricted invocations do not open a transport connection. Observatory `!spawn` ancestry, MIRC channels, and user-facing room names are not native delegation scope; they never cause implicit joins.

## Implementation and lifecycle interfaces

- `tools/native_agent_hub.py:attach_hub_capability` assembles an agent-local schema after profile/session initialization; it never registers a global core tool and never starts a server.
- `get_parent_hub` provisions lazily, keyed by absolute profile home and durable conversation ID. It starts the selected **built Mercury OMP** runtime's private `__omp_worker_native_hub` entry. A mode-0600 rendezvous in the profile's `runtime/native-hub/` directory allows a replacement parent to authenticate to the same coordinator. No provider/model calls are made.
- The coordinator owns an actual native `MircBus` and `AgentRegistry`. An authenticated localhost duplex adapter forwards native delivery envelopes to each destination's native `MircBus.receive`; the destination's existing `MircBridge` performs waiter consumption, escaping, parent steering, busy injection, wake, and lifecycle revival. No second IRC implementation or user-room routing exists.
- `NativeHubSession.child_env` emits explicit `MERCURY_A2A_ADDRESS`, `MERCURY_A2A_TOKEN`, `MERCURY_A2A_ID`, `MERCURY_A2A_PARENT`, `MERCURY_A2A_DEPTH`. The token grants only that external OMP subtree; it cannot impersonate `Main`, another subtree, another profile, or another session. Nested native OMP registry events propagate through the same connection. Peer roster frames contain identity/status/activity only, never credentials or transcript paths.
- The narrow Hermes delegation hook places these fields on internal task metadata and overlays them per child; model task fields cannot select/provision a hub. Both RPC and one-shot child paths already consume those overlays. Model selection, approval callbacks, child control handles, and Observatory feeds/rooms/history remain on their existing paths.
- SDK construction maps an externally provisioned child to its native subagent identity/depth before capability assembly and connects only when native messaging is permitted. Native OMP sessions without an external scope remain unchanged.
- The Hermes receiver implements native recipient-side FIFO inbox/waiter precedence. Busy peer data drains at the next API boundary, escaped and agent-attributed. Idle CLI/gateway hosts schedule through their existing input path; gateway delivery is pinned to the original conversation, permitting only verified compression continuation, never `/new`. Finite/library hosts can use the explicit receive loop (`hub wait`/`inbox`).
- Native terminal events and reply drains cross the adapter; awaited sends retain future-reply, clean stopped-peer, timeout, cancellation and no-double-delivery behavior. Reply drains follow the native wait/lifecycle deadline, not the short ordinary transport-request deadline; disconnect/failure settles through the terminal wait path. Hard-abort identity tombstones survive transport reconnect.
- `relay_events` and optional `_native_hub_relay_observer(event)` expose native **display-only** root observations of sibling DMs. They are not model messages, user instructions, room events or transcript/history writes. Broadcasts that directly reach Main suppress duplicate sibling-leg relays.
- `set_agent_hub_running` mirrors the authoritative parent turn; owner interruption wakes only its pending waits. `detach_agent_hub` uses exact owner identity, so stale agent closes cannot detach a replacement parent.
- `NativeHubSession.detach()` is the planned-restart/rebuild interface: release the parent connection but preserve active child coordination. The coordinator outlives parent stdin EOF while peers remain and self-reaps after an empty-scope reconnect grace (30 seconds). Reconnect does not replay accepted messages.
- `close_parent_hub(profile, conversation_id)` is **explicit conversation termination**, not restart drain. `/new`/session switch invokes the cutover helper; agent object close detaches. Lifecycle integration must never substitute hard close for planned-restart detach.
- Invocation lifetime remains caller-owned: existing Hermes RPC/one-shot helpers close their external child transport on completion/error/cancellation; Observatory native depth-1 workers are also terminal. Native adopted descendants keep their existing idle/park/revive rules. The adapter does not keep user-facing rooms alive or change restart/delegation completion ownership.

## Local verification

The focused Python suite runs parallel source OMP fixture processes through the **real native bridge and hub tools**, and separately executes the actual CLI worker plus actual SDK construction. No installed binary or paid provider is used. Native focused tests defend flat topology, another scope's exclusion, direct/broadcast delivery, FIFO consumption, failure buffering, clean terminal awaits, hard-abort reconnect rejection, coalesced parked revival, and native-parent/external-parent observable equivalence. Python tests defend profile/session isolation, disabled no-connect behavior, per-child provisioning, parent send/receive and relay visibility, owner replacement/reset, receive-race closure, and private rendezvous cleanup.

Integration commands from this worktree:

```sh
# Hermes source tests (run from hermes/)
PYTHONPATH=. /tmp/v0327-venv/bin/python -m pytest tests/tools/test_native_agent_hub.py tests/tools/test_omp_delegation.py tests/tools/test_omp_delegation_linear_mem.py tests/tools/test_omp_rpc_transport.py tests/gateway/test_plugin_message_injection.py tests/mercury_cli/test_plugin_message_injection.py -q
# OMP source tests (run from omp/)
npm exec --yes --package=bun@1.3.14 -- bun test packages/coding-agent/test/mirc/external-hub.test.ts packages/coding-agent/test/tools/irc.test.ts packages/coding-agent/test/tools/irc-roster-activity.test.ts
```

Repro commands, exact test logs, red/green evidence, message receipts and commit handles are recorded incrementally in `/tmp/mercury-v045-codex-evidence/hub.json`. The integration owner owns merged full-suite and four-target release gates.
