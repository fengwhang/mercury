# Matrix gateway bugs — root cause (diagnosis only, live evidence)

Status: diagnosis only. No VM files modified, no repo files modified.
Local repo HEAD: `8ac0783e` v0.0.44 (2026-09-12 12:46 -0400).
VM install (READ LIVE, not inferred): 0.0.44, built 2026-09-12T16:47:28Z,
source git archive `8ac0783e` — the SAME rev as local HEAD.
VM procs: gateway 1474, tuwunel 1475, sidecar 1482, all started 18:37:46 EDT
2026-09-12 (VM rebooted ~18:38; prior gateway life exited UNCLEANLY).

Correction: the first draft of this file claimed the VM ran 0.0.15 and the
fix was "just update". That was wrong — it misread the repo's checked-in
DIST_INFO.txt (stale 0.0.15) as the VM version. Live `cat` on the VM proves
0.0.44. All findings below are re-derived from VM logs + VM databases +
local code at HEAD. The bugs are REAL at HEAD, not version skew.

## 1. Bug 2a — gateway never resumes / verifies after a subagent completes (PROVED, two layers)

Concrete case: `deleg_d1ae7990` (turkey-website), dispatched from the gateway
room. The work itself SUCCEEDED — `/home/user/turkey-site/` exists
(index.html, app.js, styles.css, written 16:38). The gateway never verified it.

Layer A — gateway native watcher drops Matrix completions (gateway.log):

  18:37:57 Synthetic event source unresolvable: session_key='***'
    platform='' chat_type='' chat_id='' evt_type=async_delegation
  18:37:57 Dropping watch notification for raw session session:gateway:
    no api_server adapter to self-post through
  (repeats 18:38:01, 18:38:03, and 16:33–16:39 for earlier batches)

Mechanism (`hermes/gateway/run.py` at HEAD): the completion event carries
`origin_session='session:gateway'` with no usable `session_key`
(`_build_process_event_source`, :26987-27079: session-store miss, cache miss,
`_parse_session_key` returns None for non-`agent:main:` keys). `_inject_watch_notification`
(:27101+) then tries the api_server self-post fallback, but the VM gateway has
"**No messaging platforms enabled** ... continue running for cron job
execution" — `self.adapters` has no api_server, so it returns None and the
watcher drops the event (`_async_delegation_watcher`, :27838-27900, only
requeues on `delivered is False`; None is silently gone for this consumer).

Durable proof (`/home/user/.mercury/hermes/state.db`, `async_delegations`):

  deleg_d1ae7990  origin_session=session:gateway  parent=gateway
    state=completed  delivery_state=pending  delivery_attempts=5
  deleg_84635466  (gravity-flip)  completed / pending / attempts=7
  older rows: completed / dropped / attempts=8 (= _MAX_DELIVERY_ATTEMPTS,
    `hermes/tools/async_delegation.py:84`)

So Matrix-born completions retry until attempts exhaust, then go `dropped` —
the gateway turn NEVER re-enters through this path. CLI/SimpleX/Telegram work
because they have native adapters + structured session keys; Matrix sessions
live behind the sidecar and have neither.

Layer B — the sidecar followup (the REAL Matrix path) drops when the gateway
is busy (sidecar.log):

  2026-09-12 16:39:32 INFO __main__: delegate followup skipped:
    gateway busy, steer missed (child deleg_d1ae7990/0)

Mechanism (`hermes/observatory/sidecar_main.py`, `_followup_to_gateway`,
:3444-3471): on child death the sidecar always continues the parent
(`_maybe_post_delegate_followup`, :3411-3442). For a gateway parent it checks
`_gateway_delivery_in_flight()`; if busy it tries ONE `_steer_gateway_midturn`
(:3567-3583 → control-socket `steer` verb → `gateway_session.steer_gateway_agent`,
`gateway_session.py:1057+`). On miss it LOGS AND RETURNS — no queue, no retry,
no wait-for-idle. The child result is lost to the conversation (the room may
still get the death render; the parent turn never runs, so no verification).

Same line for `deleg_6aba10ee/0` on Sep 11. And `deleg_84635466/0` shows the
sibling gate: "delegate followup skipped: routine success (status completed)" —
the `_needs_room_reply` quiet path (:278-300) suppressing the visible message.

Root cause 2a: Matrix completions have exactly one working lane (sidecar
followup → control-socket inject), and that lane has a drop-on-busy hole with
no retry. The native gateway lane cannot serve Matrix sessions by construction
(no adapter, no session key).

## 2. Bug 2b — mid-task message does not interrupt / correct (PROVED for the followup shape; room-text path needs a live repro)

What the logs prove: `_steer_gateway_midturn` MISSED at 16:39:32 (see above) —
a steer into a live gateway turn failed and the text died instead of landing.
Sidecar log contains ZERO `gateway-steer:` successes and ZERO child `steer`
executions (only Sep-9 `control action pending transport` lines from before
the executor landed in `1299f7dd`). So mid-turn steers are not landing in
practice on this box.

Suspected mechanism (needs live confirmation, stated as hypothesis): the
gateway-room steer path exists at HEAD (`_handle_gateway_prompt_outcome`,
:3510-3565: kind==steer + in-flight → `steer` verb → redirect-then-steer
`2f898bca`/`283b5460`), but `steer_gateway_agent` steers the sidecar-visible
cached agent (`gateway_session._session_agents['gateway']`), while a busy
gateway may be running the turn on the runner's own agent object. A miss
reports `steered=False` after the 2s build-window wait and the caller drops.
Additionally `_interrupt_gateway_for_steer` (the miss fallback) is only wired
in the room-text outcome handler, NOT in `_followup_to_gateway` — the followup
hole has no fallback at all.

What is NOT broken: native adapter intake (interrupt/queue/steer + redirect
blocks at `run.py:11090-11190`, cold-path intercept :18681+, Guard-2 slash
dispatch) predates v0.0.1 (`05cd0dc4`) and serves SimpleX/Telegram/CLI. The
Matrix room bypasses all of it via sidecar InjectText → child-action path.

## 3. Bug 1 — child rooms created but no tool calls / thinking trace (PROVED: E2EE send failures + discovery crash)

Concrete case: the turkey child room `!o4fXZRp2HudRGswbXC:vm`
(`crypt:deleg_d1ae7990/0` in observatory state.meta).

- Discovery `add` for `deleg_d1ae7990` CRASHED at the lifecycle send
  (sidecar.log 16:37:03): `_apply_discovery_event` → `render_lifecycle`
  (`sidecar_main.py:1922`) → `renderer._execute` → `e2ee.send_encrypted_message`
  → `mautrix ... EncryptionError: No group session created`. The node setup
  aborted there, so the room existed (converge OK) but the lifecycle message
  never landed — the "empty room" shape.
- Same window: `E2EE STALE OTK POOL for !o4fXZRp2HudRGswbXC:vm: 1/1 recipient
  device(s) presented one-time keys that FAIL signature verification` (16:37:03
  through 16:37:14, repeated), i.e. the Element/Element-X identity-reset shape.
  A share eventually succeeded at 16:37:58 (`Group session 3wsL/... successfully
  shared`), but every subsequent phone-bound event failed to decrypt
  (`megolm decrypt failed ... no session with given ID 3wsL/...`, dozens,
  16:37:58–16:39:32), and the sidecar log holds 16,137 tracebacks total,
  dominated by E2EE send/decrypt failures.
- The feed machinery itself is PRESENT at HEAD and on the VM (child-feed
  watcher `gateway_session.py:601-682`, OmpFeed self-stream `omp_feed.py`,
  datagram render `sidecar_main.py:1753-1850`) — but sidecar.log shows ZERO
  `child_event`/OmpFeed forward lines, and every render that did attempt died
  in `e2ee.py execute`. So even working streams cannot land while shares fail.

Root cause 1: the stream path exists; the crypto path wedges it. Two defects
combine: (a) stale-OTK / post-reset shares fail (client-side reset,
server-side stale pool — needs re-converge / `mercury setup observatory` +
phone re-verify), and (b) a single failed lifecycle SEND aborts the whole
discovery-add, leaving the node half-built instead of converging the room and
retrying the send.

## 4. Bug 3 — /spawn room exists with a username but the engine is not connected (PROVED: orphaned never-materialized node)

Concrete case: `orch-3f358091` ("Wario"), omp engine, live, depth 0:

  observatory state.db: session_ref=.../omp-sessions/2026-09-12T20-36-15-109Z_*.jsonl
    (FILE DOES NOT EXIST), extra={"model": null, "session_materialized": false}
  omp-sessions/ dir: only a Sep-11 session; no Wario file.
  procs: NO `omp --mode rpc` anywhere.
  sidecar.log 14:37:48 (boot respawn pass): "respawn: node orch-3f358091
    failed: omp session file ... never materialized (first turn pending —
    run on the live spawn handle, never resume)" — correct refusal
    (`respawn.py` never-materialized rule).

Chain: Wario spawned ~16:36 EDT (created_epoch 1789245376); its engine handle
lived ONLY in the gateway process's in-memory registry
(`platform_hook.LAST_BOOT.registry`, adopted via `_adopt_live_spawn_handle`,
`sidecar_main.py:2743-2801`). The VM rebooted ~18:37 (gateway
lifecycle_ledger: "Previous gateway life exited UNCLEANLY (no exit path ran —
SIGKILL / OOM / VM death)"); both processes restarted at 18:37:46; the live
handle died with the old gateway; the session file never materialized because
no first turn ever completed. Result: a live room/space/MXID with no engine
behind it. Any message there resolves `_child_handle` → miss → the
"session is unavailable" notice (same string as the Sep-11 claros/grumio
report; sidecar.log also shows that shape for `orch-77f8998b` repeatedly:
"omp child prompt dropped: no handle ... session file is gone").

Root cause 3: spawn durability gap — a 0-agent that never completes its first
turn has no durable session, only an in-memory handle; any gateway restart
before turn one orphans it. D18 respawn correctly refuses (no session to
resume), but nothing marks, exits, or rebuilds the orphan, so the room sits
live and empty.

## Fix proposal (for approval — DO NOT run yet)

1. 2a-followup (critical): `_followup_to_gateway` must not drop on
   busy + steer-miss. Preferred: wait for the in-flight delivery task (bounded,
   e.g. reuse stall-grace) then inject as a normal followup turn; fallback:
   enqueue one queued inject with the existing queued/applied ledger so it
   lands exactly once. Files: `hermes/observatory/sidecar_main.py:3444-3471`
   (add wait/retry), no schema change.
2. 2a-native (secondary): stop the log-spam retry loop for Matrix sessions —
   either stamp Matrix-dispatched batches with a routable marker the watcher
   understands, or route `session:gateway`-origin completions straight to the
   sidecar followup and let the native watcher skip them. Files:
   `hermes/gateway/run.py:26987-27190` (`_build_process_event_source` /
   `_inject_watch_notification`), `hermes/tools/omp_delegation.py:1454-1466`
   (dispatch stamping).低いリスク: logging-only first, then routing.
3. 2b-steer: confirm the miss hypothesis with a live repro (mid-turn room
   message + check `gateway-steer:` vs `gateway-steer-fallback:` in
   routing_log), then make `steer_gateway_agent` reach the runner's live agent
   (not just the cached inject agent) and add the interrupt fallback to the
   followup path that room-text already has. Files:
   `hermes/observatory/gateway_session.py:1057-1112`,
   `hermes/observatory/sidecar_main.py:3567-3600`, `:3444-3471`.
4. Bug 1: (a) re-converge E2EE on the VM (`mercury setup observatory` +
   phone force-quit/rejoin + re-verify; stale-OTK pool is client-reset
   fallout); (b) code: discovery-add must converge state + room even when the
   first SEND fails, and retry failed sends (never abort node setup on a send
   error). Files: `hermes/observatory/sidecar_main.py:1874-1925`
   (`_apply_discovery_event`), `hermes/observatory/renderer.py:808+` (render
   paths — return intents durable, send best-effort with retry).
5. Bug 3: (a) immediate: `/exit` the orphaned Wario node (or delete the row)
   so the dead room stops misleading; (b) code: spawn must materialize the
   session at spawn time (first-turn ping or pre-created JSONL/row) OR
   `_child_handle`'s never-materialized fresh-build path must actually fire
   for post-restart orphans and repoint state; plus boot should mark
   never-materialized + handle-less 0-agents visibly (notice in room, not
   silent live-empty). Files: `hermes/observatory/spawn.py:496-617`,
   `hermes/observatory/sidecar_main.py:2620-2741`, `hermes/observatory/respawn.py`.
6. Hygiene: fix the stale `wire_siblings` docstring (`sidecar_main.py:928-934`)
   still claiming fan-out is "not landed"; refresh the checked-in
   `DIST_INFO.txt` (still 0.0.15 in repo while releases are at 0.0.44) so the
   next investigator does not chase the skew ghost.

## Verification performed (all read-only)

- VM DIST_INFO 0.0.44 / source 8ac0783e (live cat via `sudo -u user`); procs
  1474/1475/1482 started 18:37:46; tuwunel `server_name="vm"`; both sockets live.
- Gateway.log: unresolvable-source + no-adapter drops (quoted above); "No
  messaging platforms enabled"; prior life UNCLEAN exit.
- hermes state.db: turkey + gravity-flip completed/pending (attempts 5/7),
  older rows completed/dropped (8/8); gateway session `gateway` live, 1223 msgs;
  last turns: user "stop" → delegate_task list (0 children) → "Stopped — …".
- turkey-site/ files exist (work done, never verified in chat).
- Sidecar.log: turkey discovery-add traceback (E2EE No group session);
  STALE OTK POOL → share → mass decrypt failures; followup steer-missed drops
  (turkey + Sep-11 sibling); 16k tracebacks dominated by E2EE + Sep-9
  tuwunel-403/aiohttp boot failures (historical, resolved); Wario respawn
  refusal; zero successful steer/fanout lines.
- Observatory state.db: gw + Wario live; Wario session_ref missing,
  materialized=false; crypt entries for both child rooms.
- No repo files changed by this investigation except this doc (uncommitted);
  no VM files changed (all access `sudo -n -u user` reads + sqlite selects).

## Appendix — background probes (for the record)

- deleg_8931a2a8 (stream/spawn probe): correctly mapped the HEAD machinery
  and commits, but concluded "VM is 0.0.15, just update" — wrong premise
  (stale DIST_INFO misread). Its file:line map was used and re-verified above.
- deleg_ec606602 (resume/interrupt probe): correctly called 2a "genuinely
  broken at HEAD" with the completion-queue → adapter-acceptance drop seam and
  the session-key colon hazard — live logs confirm the drop shape (unresolvable
  source + no adapter). Its hash dating (all fixes post-0.0.15) is accurate but
  moot now that the VM is 0.0.44.
