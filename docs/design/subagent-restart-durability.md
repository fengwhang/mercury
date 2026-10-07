# Subagent restart durability

## Authority and ordering

The async delegation SQLite ledger is the task/result authority; Observatory and mLounge rooms are views, not task ownership. Each dispatch retains its originating parent session, route, original goal and task specification. Each child is addressed by `<delegation_id>/<task_index>` and records gateway and child PID/start fingerprints, workdir, selected profile, transport and transcript checkpoint. Checkpoints do not store environment variables or credentials.

A restart request checkpoints active delegations before waiting for an after-turn restart. The configured bounded drain includes async delegations as well as foreground, cron and API work. Shutdown checkpoints again before cancellation/termination. Exhausting the drain does not turn unfinished work into success.

A terminal result commits before its completion is enqueued, before transport shutdown and before room retirement. The first persisted terminal outcome wins; a later interrupted cleanup cannot overwrite a completed result or reopen its delivery.

## Process and transcript evidence

A PID alone is insufficient. Linux start time is parsed relative to the final `/proc/<pid>/stat` comm delimiter, so spaces or parentheses in the process name do not change the fingerprint. A missing or unreadable fingerprint is `unverified`, not proof of death. PID reuse proves the original process is gone without authorizing signals against the replacement.

Delegated native OMP RPC and print modes write child-bound custom JSONL records:

- `mercury_delegation_started`: invalidates any earlier terminal checkpoint.
- `mercury_delegation_terminal`: records completed/failed/interrupted and the complete successful final text, synchronously flushed before publishing terminal `agent_end` or stdout.

Only terminal `agent_end` closes a task. A final assistant message may precede queued work. Final assistant text, `session_exit`, transport disposal and room silence are not completion proof. A later task start, message, branch/compaction or torn record invalidates a stale checkpoint. One-shot children use a private per-child session directory; recovery accepts only one unambiguous child-bound result.

On restart, a provably live owner retains ownership. An unverified owner is not stolen. For a proven-dead owner:

- A child with terminal ledger or valid native checkpoint evidence gets its real terminal outcome.
- A running or unverified child process is adopted by a compare-and-set owner transfer and monitored without killing or age-reaping it. Monitoring uses the original profile's ledger context.
- A child proven dead without terminal evidence is interrupted, with no fabricated successful summary.
- Missing child spawn identity does not authorize terminalizing its child row or retiring its room.

Adoption restores durable monitoring and parent completion routing, not the old process's RPC stdin/control channel. Interrupted results carry the original task goal, checkpoint, workdir and transcript location plus explicit safe reconciliation instructions. The parent can inspect committed work/transcripts and resume the remaining goal; the gateway does not blindly replay potentially side-effecting tasks.

## Recoverable completion acceptance

A delivery claim stores consumer PID/start identity. A live or unverified claimant cannot be stolen based on elapsed time; a proven-dead claimant can be recovered. Delivery remains pending until the originating parent's typed internal user notification is durably stored. Adapter scheduling alone is not acceptance.

Each injected async completion has stable message identity `async-delegation:<delegation_id>`. Recovery checks the original parent and its compression tip for that identity or the exact canonical internal notification. Consolidated notifications contain each child's canonical block. A receipt survives the gateway dying between parent acceptance and producer acknowledgement: replay acknowledges the existing receipt rather than injecting a second turn. It also settles an old unverifiable claim when durable acceptance is already proved. Acknowledgement clears claim ownership fields.

## Room lifecycle

Boot reconciliation runs after the Observatory bot has joined and before boot reaping. It replays persisted terminal outcomes to their own rooms with a bounded marker wait, then applies room expiry. Terminal marker delivery is recorded for retained rooms to avoid repeated markers.

- Depth 1: retire when the task terminates.
- Depth greater than 1: retain the terminal room until its parent terminates.
- Depth 0: retain until explicit user `!exit`.
- Running or unverified tasks: retain; neither silence nor age is retirement evidence.

A watcher disappearing from the local process registry is not proof of success. Room retirement waits for the durable task fate. Explicit MIRC `state_dir` selects the authoritative room registry, even when a cached RoomManager belongs to another profile. Boot pins depth-0 roots, not every previously joined terminal channel.

## Tool-result rendering

Native OMP emits tool invocation and tool-result message events separately. The feed preserves that distinction. Normal Hermes `tool.completed` callbacks, including the top-level collector, now produce a role-tool output frame (`tool_result` at the collector seam). The shared IRC renderer labels that frame `result:`. Only invocation frames use the invocation glyph; todo plan updates remain status frames.

## Native A2A hub integration seams

These hooks are independent of the native A2A IRC hub implementation and of mLounge room routing:

| Hook | Ownership / purpose |
| --- | --- |
| `async_delegation.record_child_checkpoint` | Immutable task/profile/workdir intent before launch |
| `async_delegation.record_child_spawn` | Child PID/start/session evidence at transport registration |
| `async_delegation.record_child_terminal` | Observed task fate before shutdown/notification |
| `async_delegation.checkpoint_active_delegations` | Planned drain checkpoint |
| `omp_delegation._run_omp_task` | Establishes child checkpoint and native terminal binding |
| `omp_rpc_transport.run_omp_task_rpc(task_result=...)` | Result barrier before transport stop |
| `recordRpcDelegationEvent` | Native RPC/print task-terminal flush barrier |
| `RoomManager.reconcile_terminal_children` | Terminal marker replay after bot join, before reaping |

`MERCURY_DELEGATION_CHILD_ID` binds native terminal checkpoints only; it is not an A2A topology or routing implementation. `PI_CODING_AGENT_SESSION_DIR` selects the private durable session directory. Hub changes should preserve these barriers and exact child IDs rather than deriving fate from IRC connection state.

## Diagnosis and verification

Historical room reconnects correlate with gateway `SystemExit(75)` and replacement gateway PIDs. Installed gateway drain logs show zero counted active work followed by interruption of background delegations; async delegates were absent from the drain count. The historic initiating signal sender/requester is not proved by those logs. Unrelated worktree edits are not evidence of an automatic source watcher. New restart requests log their trigger (`signal:SIGUSR1`, `control:pause-for-update`, or direct request) and checkpoint count. Skipping planned exits in the stuck-loop counter alone does not repair child drain or ownership recovery.

Focused regressions cover PID reuse and unknown identity, honest terminal evidence, private one-shot checkpoints, queued continuations, delivery claims, typed parent receipts, root/deep room laws, pending terminal markers, configured registry isolation and both tool-result paths. The model-free process fixture uses real owner/child/recovery OS processes in a temporary home: it kills the owner, completes or interrupts a child, kills recovery after parent persistence but before producer acknowledgement, then restarts and checks one durable receipt, one adapter call, preserved running/root rooms and recovered transcripts. Native tests exercise real SessionManager flush and print subscription ordering without provider/network calls.
