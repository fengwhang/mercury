# Internal agent hub

Audit pins: Mercury `de312dad74dc198688fefba8434eab90a9b49bb3` (declared vendored OMP `d2d2c17`, v18.1.6), upstream OMP `2f9d6d6b2494c89d422a18c2899f715f41a2ad67` (v18.8.2). Upstream cached reference `6d8552d7f9df1852826923f07f0eed4fe29511f3` (v18.6.0).

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

Upstream v18.8.2 splits messaging into the `irc` module and TUI tool packages; `send` no longer has vendored `await/replyTo` sugar. Flat roster, local mailbox, async delivery and parked revival remain. Mercury retains its vendored API; this feature is not an upstream version upgrade.

Source links: [upstream bus](https://github.com/can1357/oh-my-pi/blob/2f9d6d6b2494c89d422a18c2899f715f41a2ad67/packages/coding-agent/src/irc/bus.ts), [upstream messaging](https://github.com/can1357/oh-my-pi/blob/2f9d6d6b2494c89d422a18c2899f715f41a2ad67/packages/coding-agent/src/irc/messaging.ts), [upstream registry](https://github.com/can1357/oh-my-pi/blob/2f9d6d6b2494c89d422a18c2899f715f41a2ad67/packages/coding-agent/src/registry/agent-registry.ts).

## Missing seam

Hermes `tools/omp_delegation.py:_run_omp_task` launches one process per child. Each creates its own `Main` and process-global registry, so identical hub tools cannot reach siblings or the Hermes parent. Observatory node IDs/rooms are presentation, not the missing native mailbox scope. The adapter must bridge the native bus and registry at the session capability boundary, with one local conversation scope and per-child identities, without changing Observatory feeds, room history, model/provider calls, or approval handling.
