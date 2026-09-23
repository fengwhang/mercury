# v0.0.47 live failures — root causes + fix plan (Matrix child rooms still empty)

Date: 2026-09-13. VM: fedora @ 192.168.122.250, DIST_INFO 0.0.47,
source fa66e6e7, sidecar 9645 / gateway 10917 / tuwunel / omp RPC 11164.
Code under review: `main` at fa66e6e7 (v0.0.47 tag, release 387801741).
Status: UNCOMMITTED diagnosis. No repo/VM code touched.

User-visible symptom: delegate_task subagent rooms, /spawnomp rooms and
/spawn rooms are all still empty — identical to v0.0.46. One real
improvement: the gateway DOES continue its turn after a subagent finishes
(the followup wait-or-queue path works; `async_delegations` rows read
`completed`).

## 1. The headline: v0.0.47 code never executes live

`sidecar.log` (309977 lines, current through 01:52:50 today) contains ZERO
lines mentioning `omp feed`, `child_event`, `child_lifecycle`,
`turn_frames`, `_consume_omp`, `_takeover_omp`, `replay_child` — while the
live tree HAS the code (`_consume_omp_turn_feed` at
sidecar_main.py:2283, `_takeover_omp_feed` at :3034, `agent_turn_frames`
at omp_feed.py:704, all grep-confirmed on the VM). The new machinery is
deployed but never runs. Tests were green (85 targeted, 1389
observatory) because they exercise the machinery with fakes; production
never reaches it.

Why it never runs — two facts from the live DB:

- `observatory/state.db nodes`: only 3 rows — `gw|hermes|live`,
  `orch-9bb94fb9|hermes|live` (`session_materialized=false`),
  `orch-7e8015f0|omp|live` (`session_materialized=false`,
  `session_ref=.../2026-09-13T05-47-32-892Z_01a0994e-....jsonl`).
  That session file DOES NOT exist on disk (`omp-sessions/` holds only
  two older jsonl files). No delegation child node exists at all.
- Neither 0-agent ever completed a first turn. No first turn = no feed,
  no trace, no reply. The question is why no turn ever starts, and why
  nothing (not even an error) ever renders.

## 2. Root cause #1 (inbound): child-room messages never decrypt, so no turn ever routes

Owner's messages in child rooms arrive as `m.room.encrypted` but always
fail: `megolm decrypt failed ... no session with given ID TBizJX...`
(repeated 20+ times 01:52:19–01:52:50 for room `!aaLkYb37C5WCNIep8l:vm`).
`_on_transaction` (sidecar_main.py:2408) then calls
`_notice_decrypt_failure` (:2433) and routes the STILL-ENCRYPTED event —
`route()` drops it (`drop:bad-content`, no body). Net: user text in a
child room never becomes a prompt. No prompt = no turn = empty room.

Three stacked defects behind this:

(a) `decrypt_event` (e2ee.py:2418) tries ONLY the gateway machine:
`crypto = self.machine_for(self.gateway_mxid or ...)`. Child rooms are
voiced by child ghosts; the crypto dir holds only
`merc_gateway-agent.db` + `merc_waluigi-site.db` — the current child
ghost has no Olm account at all. A room key shared TO the child ghost
device can never be opened by the gateway machine lookup used here.

(b) Child ghosts never get crypto at spawn. `warmup` (e2ee.py:1644) is
explicitly gateway-ONLY ("never the owner MXID ... Without a gateway
identity there is nothing safe to warm"). `_register_spawn_ghost`
(spawn.py:463) registers the ghost as a homeserver user (fixing the old
`M_INVALID_PARAM: Called create_device for non-existent user` 400) but
creates no Olm machine, publishes no device/OTK keys. The owner's phone
therefore has no keys for the child ghost (`No one-time keys nor device
keys got when trying to share keys` at boot 01:40:46) and cannot
establish Olm to it.

(c) The decrypt-failure notice can never land in a child room.
`_notice_decrypt_failure` sends as `gateway_mxid` and skips when the
gateway is not a member — the gateway NEVER joins child rooms by design.
So the one notice that would tell the user "your message didn't decrypt,
here's the remedy" is silently skipped exactly where it's needed most.
This is why rooms show NOTHING rather than an error. Fix: voice the
notice as the room's member ghost (the child mxid from state, same
pattern as `_member_reader_for` / `_post_notice`'s child voice).

## 3. Root cause #2 (outbound): child-room sends fail closed, retry churns without healing

29x `live send failed — queued for retry: No group session created`.
The STALE-OTK guard (`verify_recipient_otks`, e2ee.py:2002;
`ensure_room_share`, e2ee.py:2107) SKIPS the share when the owner's
phone OTKs fail signature verify (devices U9K6Xi9Qap today,
YJqZVYmuiE on 09-12 — same Element/Element X identity-reset shape:
server keeps serving OLD-signed OTKs after the phone rotated its
identity key under the same device id).

Consequences:
- Every child-room render (trace frames, replies, notices) fails and
  parks in `_pending_sends` (renderer.py:749).
- The ONLY drain is `retry_pending_sends` (renderer.py:758), called
  solely from `_apply_discovery_event` (sidecar_main.py:~1981) on the 2s
  discovery poll. It re-attempts, re-fails (cause persists), re-queues
  until `SEND_RETRY_MAX` then DROPS with a warning. Churn, not healing:
  nothing re-claims OTKs, nothing re-shares on `to_device` arrival
  (room keys DO arrive — `e2ee crypto routed {'to_device': 1, ...}`
  fires repeatedly — but no drain is hooked to that event).
- Even the v0.0.47 delegation replay (`replay_child_turn_frames` →
  `push_child_feed_event` datagrams → `_handle_child_feed_datagram`
  renders) dies here: renders succeed logically, sends fail physically.

Note the paradox in the log proving the two halves are inconsistent:
`Group session TBizJX... for !aaLk... successfully shared` at
01:52:02 followed by endless `no session with given ID TBizJX...` on
decrypt — we shared OUR outbound session, but inbound (owner→us) needs
the OWNER's room key delivered to one of OUR machines, which never
happens per #2.

## 4. Root cause #3: Matrix completion lane still drops (`delivery_state=pending`)

All recent delegs: `deleg_33512f7c|completed|pending|1`,
`deleg_3c798890|completed|pending|5`, `deleg_97b18adf|completed|pending|5`;
older ones `dropped|8`. Gateway log at 01:52:50 TODAY:
`Synthetic event source unresolvable ... Dropping watch notification for
raw session session:gateway: no api_server adapter to self-post through`.
The v0.0.45 followup change made the gateway continue (the improvement
the user sees), but the durable `delivery_state` never leaves `pending`
— the Matrix-born completion has no route. Cosmetic today, load-bearing
tomorrow (retry/ack accounting keys off it).

## 5. Root cause #4: gateway child-feed watcher liveness unverified

`ensure_child_feed_watcher` (gateway_session.py:799) is called inside
the control-socket setup try/except in gateway/run.py:33341 that
debug-logs failure and continues. Gateway log shows the control socket
listening but ZERO `observatory-child-feed` lines (all watcher logging
is `logger.debug`). Thread comm names are all `mercury` (useless).
Delegation children live ONLY in the gateway's `_live_children` table —
without this watcher there are no `child_lifecycle`/`child_event`
datagrams, no sidecar child nodes for delegations, no
`replay_child_turn_frames`. The sidecar turn-reader path
(`_run_omp_child_prompt`) additionally has no handle for
gateway-delegation children (sidecar registry ≠ gateway table), so even
healthy crypto would leave delegation rooms dark unless the
watcher+replay path runs. First step is observability (info-level
start/attach/stop lines + a health counter), then verify on the live
gateway before changing behavior.

## 6. Root cause #5: tuwunel itself 500s

homeserver.log: `PUT .../rooms/.../send/m.room.encrypted/... 500`,
`GET .../state 500`, `POST .../join/... 500`, `GET .../messages 500`
(latest 05:52:50Z). Some sends/notices/joins fail server-side
regardless of client code. Needs homeserver-log triage (full
tracebacks around those lines) before attributing every failure to our
stack; may need a tuwunel restart or version look.

## 7. Fix slices (each: red test → minimal fix → green suite → commit)

S1. Child-ghost crypto at spawn/converge. After
`_register_spawn_ghost` (spawn.py:463) and in the datagram child paths
(`_ensure_datagram_child_node`, sidecar_main.py:1686):
`machine_for(child_mxid)` + `load()` + publish device/OTKs
(`share_keys` path, cf. warmup e2ee.py:1644). Never the owner mxid.
Tests: spawn mints machine + keys (fake homeserver), no owner machine
created.

S2. Inbound decrypt fallback. `decrypt_event` (e2ee.py:2418): try the
room's member-ghost machines (state lookup by room_id) before/fallback
after the gateway machine; return first success. Tests: matrix of
(gateway-key-only, ghost-key-only, neither) → decrypts/decrypts/None.

S3. Voice decrypt-failure notices as the room ghost.
`_notice_decrypt_failure` (sidecar_main.py:2433): resolve sender =
room's member mxid from state (child voice), gateway only as fallback;
membership check against THAT sender. Tests: child-room decrypt failure
posts into the child room as the child ghost; gateway rooms unchanged.

S4. Retry drain that heals. Hook `retry_pending_sends` to
`to_device`-routed transactions (new keys may have arrived) in addition
to discovery events; add a bounded timer drain (e.g. 30s) so rooms heal
with no new events; on drain-after-fresh-keys, force re-claim + re-share
(`ensure_room_share`) before re-sending rather than replaying the same
doomed session. Tests: queued send delivers after simulated key arrival;
timer drain fires; SEND_RETRY_MAX drop still bounded.

S5. Stale-OTK escape hatch. When the guard finds ALL recipient devices
stale (zero verified): keep refusing silent success, but (i) post ONE
user-visible notice per room per process with the phone-side remedy,
(ii) schedule re-claim with backoff instead of burning SEND_RETRY_MAX
on an unhealable session. Tests: all-stale → no share + exactly one
notice + backoff scheduled; mixed → share to verified only (existing
behavior pinned).

S6. Matrix completion route. Give `session:gateway` completions a route
(adapter or direct inject) instead of the
`no api_server adapter ... Dropping` path (gateway/run.py watcher area);
`delivery_state` pending→delivered on Matrix like other lanes. Tests:
synthetic Matrix completion delivers + flips durable state.

S7. Watcher observability first, behavior second. Promote watcher
start/feed-attach/lifecycle-push/stop to info with child ids; add a
`child_feed_health` counter (known/feeds/pushed/dropped). Verify on VM
log BEFORE further watcher behavior changes. Tests: health counters
move in fake-table tests.

S8. Tuwunel 500 triage (read-only first). Pull full tracebacks around
the 500 lines in homeserver.log; classify (DB lock? state bug?
resource?). Remedy proposal separate — no homeserver changes in this
wave.

Non-goals: no renderer/control/spawn-schema redesign; no cross-signing
wiring (still honest-loud per e2ee REMAINING WORK #4); no plaintext
fallback in encrypted rooms; no new slash commands; room deletion rules
unchanged.

## 8. Suggested order

S7 (observe) → S1+S2+S3 (crypto+notice: rooms go from silent to alive) →
S4+S5 (drain+hatch: sends heal instead of churn) → S6 (completion
accounting) → S8 (tuwunel, read-only) → ship v0.0.48 → VM verify
(delegate_task room streams SELF trace; /spawnomp + /spawn first turns
render; steer from child room lands; stop gets CLI-parity ack).
