# Subagent restart durability

## Authority and ordering

The async delegation SQLite ledger is the task/result authority; Observatory and mLounge rooms are views, not task ownership. Each dispatch retains its originating parent session, route, original goal and task specification. Each child is addressed by `<delegation_id>/<task_index>` and records gateway and child PID/start fingerprints, workdir, selected profile, transport and transcript checkpoint. Checkpoints do not store environment variables or credentials.

A restart request durably records its provenance and checkpoints owned work before acceptance. Automatic convenience/onboarding/update restarts wait for foreground, cron, API, async-controller, native-worker and registered native-peer work to become idle; they keep the original feeds and parent completion receiver usable while deferred. Explicit administrative restart retains the configured bounded checkpoint/drain path. Unknown-sender `SIGUSR1` cannot authorize interrupting work and uses automatic admission. Shutdown fences checkpoints again before interruption, restart-only detach, service cleanup and the clean-exit marker. The supervisor completion event is emitted last.

A terminal result commits before its completion is enqueued, before transport shutdown and before room retirement. The first persisted terminal outcome wins; a later interrupted cleanup cannot overwrite a completed result or reopen its delivery.

## Restart provenance and admission

`logs/gateway-restart-requests.jsonl` is a private append-only, file/directory-synchronized journal. Each request records a correlation ID, gateway PID/start, source/reason, automatic versus administrative intent, relevant delegation IDs and available actor context before flags change. Accepted, deferred, rejected, stopping and exit-ready decisions are separate records.

Local control actors come from the server's Unix peer credentials, not request parameters. Their birth fingerprint, bounded allowlisted CLI ancestry and actual service-cgroup relationship are recorded without raw argv, environment or credentials. Unsupported transport identity remains explicitly unavailable. Platform restarts retain the authorized platform-user context; the Observatory helper carries only the original request ID for correlation. That ID is not authority.

`restart-when-idle` has no force fallback. `pause-for-update` declines active work and requires explicit `deferred:false, active_work:0` before any runtime/source mutation or Windows service cleanup. Same-unit administrative convenience calls cannot force busy work without the explicit checkpoint-resume path. Provenance/checkpoint failure refuses admission; a failure before teardown leaves the original gateway and feed guards running.

## Process and transcript evidence

A PID alone is insufficient. Linux start time is parsed relative to the final `/proc/<pid>/stat` comm delimiter, so spaces or parentheses in the process name do not change the fingerprint. Transport registration captures that birth once; final transcript refresh never replaces it with a reused PID's birth. A missing or unreadable fingerprint is `unverified`, not proof of death. PID reuse proves the original process is gone without authorizing signals against the replacement. Native process ownership, not a room or mailbox roster, participates in restart admission.

Delegated native OMP RPC and print modes write child-bound custom JSONL records:

- `mercury_delegation_started`: invalidates any earlier terminal checkpoint.
- `mercury_delegation_terminal`: records completed/failed/interrupted and the complete successful final text, synchronously flushed before publishing terminal `agent_end` or stdout.

Only terminal `agent_end` closes a task. A final assistant message may precede queued work. Final assistant text, `session_exit`, transport disposal and room silence are not completion proof. A later task start, message, branch/compaction or torn record invalidates a stale checkpoint. One-shot children use a private per-child session directory; recovery accepts only one unambiguous child-bound result.

On restart, a provably live owner retains ownership. An unverified owner is not stolen. For a proven-dead owner:

- A child with terminal ledger or valid native checkpoint evidence gets its real terminal outcome.
- A running or unverified child process is adopted by a compare-and-set owner transfer and monitored without killing or age-reaping it. Monitoring uses the original profile's ledger context.
- A child proven dead without terminal evidence is interrupted, with no fabricated successful summary.
- Missing child spawn identity does not authorize terminalizing its child row or retiring its room. The child outcome is `unverified`, not a fabricated interruption or success; the lost controller is interrupted. Batch headings report that actual fate rather than claiming every fan-out completed.

Adoption restores durable monitoring and parent completion routing, not the old process's RPC stdin/control channel. Interrupted results carry the original task goal, checkpoint, workdir and transcript location plus explicit safe reconciliation instructions. The parent can inspect committed work/transcripts and resume the remaining goal; the gateway does not blindly replay potentially side-effecting tasks.

## Recoverable completion acceptance

A delivery claim stores consumer PID/start identity. A live or unverified claimant cannot be stolen based on elapsed time; a proven-dead claimant can be recovered. Delivery remains pending until the originating parent's typed internal user notification is durably stored. Adapter scheduling alone is not acceptance.

Each injected async completion has stable message identity `async-delegation:<delegation_id>`. Recovery checks the original parent and its compression tip for that identity or the exact canonical internal notification. Consolidated notifications contain each child's canonical block. Historical typed batch receipts retain their unique accepted identity through the honest-heading cutover; the old misleading renderer is not retained. A receipt survives the gateway dying between parent acceptance and producer acknowledgement: replay acknowledges it rather than injecting a second turn. It also settles an old unverifiable claim when durable acceptance is already proved. Acknowledgement clears claim ownership fields. An explicit `cli_close` is a user boundary, not an API-server wake route; its archived result remains available without a false delivery acknowledgement.

## Room lifecycle

Boot classifies execution fate before recreating child identities. For terminal markers it joins only the bot to the existing child/parent channels and waits for the real server `366` membership receipt before changing state or applying expiry. Rejected membership defers marker/expiry rather than faking a successful send. The live snapshot is refreshed after reconciliation; retained terminal or unverified descendants do not acquire new send-only identities merely because their old row is `live`.

- Depth 1: retire when the task terminates.
- Depth greater than 1: retain the terminal room until its parent terminates.
- Depth 0: retain until explicit user `!exit`.
- Running or unverified tasks: retain; neither silence nor age is retirement evidence.

A watcher disappearing from the local process registry is not proof of success. Room retirement waits for the durable task fate. Explicit MIRC `state_dir` selects the authoritative room registry, even when a cached RoomManager belongs to another profile. Boot pins depth-0 roots, not every previously joined terminal channel.

A send-only identity announces `transport connected.`, not worker execution. Execution readiness requires the actual PID/start owner plus attached SELF/event feed. A network reconnect preserves that running worker and subscription; transport state alone does not establish task readiness or success.

Live and replayed frames share one receipt-aware transaction on the canonical gateway loop. A queued RoomManager call is not a delivery receipt: only an accepted transport send reserves the dedupe identity. Failed or unavailable sends remain replayable, including tool-completion status. Reconnect replay preserves the original worker subscription and RPC listeners; it does not detach the feed or suppress identical later work through stale live-frame reservations.

## Tool-result rendering

Native OMP emits tool invocation and tool-result message events separately. The feed preserves that distinction. Normal Hermes `tool.completed` callbacks, including the top-level collector, now produce a role-tool output frame (`tool_result` at the collector seam). The shared IRC renderer labels that frame `result:`. Only invocation frames use the invocation glyph; todo plan updates remain status frames.

## Native A2A hub integration seams

The native A2A hub is a process-local flat mailbox/registry, not network IRC. Its authenticated local adapter is distinct from mLounge/MIRC rooms. Native coordinator `setsid` does not escape the gateway's service cgroup; neither `KillMode=process` nor an untracked orphan is the lifecycle solution.

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

`gateway.restart_owners` provides the registered service-owner boundary:

| API | Contract |
| --- | --- |
| `register_owner(owner_id, profile_home=..., pid=..., started_at=..., active_work=..., checkpoint=..., detach=...)` | Pins a real coordinator identity and immutable registration token. Cached activity counts running/unsettled peer work, not idle coordinator presence. |
| `unregister_owner(token)` | Removes only that runtime generation; a stale close cannot remove its replacement. |
| `active_work_count()` | Counts authoritative registered activity; proven dead/reused processes are not active. Unreadable activity conservatively defers maintenance. |
| `checkpoint_owners(reason)` | Owner callbacks persist their actual private task/grant/message state. The lifecycle verifies/synchronizes the private manifest and records path/digest/owner identities in `<profile>/runtime/restart-owners.json`. No grant contents or credentials enter that receipt. Registry mutation or durability failure blocks the barrier. |
| `detach_owners()` | Runs the exact owner's restart-only detach once, after a matching checkpoint; never closes the conversation or revokes grants. |
| `pending_checkpoints(profile_home)` | Validates recovery receipts. The native owner chooses authenticated re-adoption of a live coordinator or real reconstruction after service death. No blind side-effect replay. |
| `complete_owner_resume(owner_id, profile_home=..., sha256=...)` | CAS-consumes a receipt only after the native owner genuinely restores/reattaches its task and message obligations. |
| `discard_checkpoint(owner_id, profile_home=...)` | Explicit `/new`/conversation exit/canonical `!exit` removes the resume receipt. The native owner separately revokes its actual grants and private state. |

Native-owner callbacks are the grant/mailbox authority. They must keep accepted work durably recoverable after a snapshot, or quiesce acceptance at the final drain fence. A checkpoint callback must not detach or stall ongoing work during an automatic deferral. Planned restart invokes `NativeHubSession.detach`/`detach_agent_hub`, never `close_parent_hub`; explicit conversation termination owns hard close and grant revocation even when the old runtime owner already detached.

Gateway admission, bounded drain and the final clean-exit barrier count these registered owners. Their checkpoints precede task interruption, agent cleanup and service-cgroup cleanup. A clean marker requires settled execution owners and successful durability barriers; `run_forever` cannot release the supervisor first. Native repair integration must register before admitting peer work and restore/discard receipts at its real generation/exit boundary. Combined acceptance kills the actual coordinator and peer service descendants and verifies restored original goals, accepted messages/replies, dedupe, feed visibility and explicit-exit revocation—not merely stdin-EOF survival.

## Diagnosis and verification

Exact historical `deleg_97e27028` forensics recover session `01a11394-8c1f-7493-b04a-ff76bcafff71`: last persisted tool result at `2026-10-06T23:39:57.568Z`, later room-only thinking at `23:40:18.558Z`, and no durable terminal/error/abort. The owner exited 75. All four subsequent `online.` markers correlate with replacement gateway boots and send-only identity hydration, not task resumes. The original child PID/start cannot be retroactively recovered; systemd's killed OMP PID `2654440` is not asserted to be this child. The recovered unknown notice did reach its IRC parent at `23:40:53`; the raw CLI drop is a separate explicitly closed session.

The four originating restart intervals contain gateway-cgroup manager reloads. Daemon reload alone does not prove restart causation; the historical requesting actor/argv remains unknown. No verified stale-worktree watcher cause is claimed. The installed foreground-only drain omitted async owners, then interrupted background batches before writing clean state. Planned-exit counter protection alone does not repair that ownership loss.

Focused regressions cover PID reuse and unknown identity, strict terminal checkpoints, private one-shot transcripts, queued continuations, typed parent receipts and the acceptance/ack split, native execution-owner activity, private registered-owner manifests/CAS restoration, authenticated restart provenance, bounded administrative drain, automatic maintenance deferral, actual JOIN receipts, room laws and both tool-result paths. Real temporary-home process fixtures kill original owners and service-like descendants, preserve completed side effects, resume only remaining saved-goal work, and verify one parent delivery. Real IRC/RPC loopback fixtures retain worker PID/feed across reconnect and publish the terminal marker before expiry. Native tests exercise SessionManager flush and print/RPC terminal ordering without provider calls. Registered-owner fixture restoration is lifecycle-contract proof; real repaired native-hub service restoration is a separate combined gate.
