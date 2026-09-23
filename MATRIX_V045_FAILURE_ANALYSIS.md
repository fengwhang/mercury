# Matrix gateway v0.0.45 — post-release failure analysis (live evidence)

Status: diagnosis from LIVE 0.0.45 evidence. No VM files modified, no repo
files modified by this doc.
Local repo HEAD: `7fabe49e` (main, v0.0.45 wave merged).
VM install (read live): 0.0.45, source `4cdaacec`, procs restarted 20:25
(sidecar 15678, tuwunel 15703, gateway 16948) after `mercury update`.
Fresh test traffic tonight: user spawned `ace` (hermes) + `king` (omp),
dispatched cow subagent `deleg_97b18adf` (completed, files exist in
`/home/user/cow-site/`), sent `stop` twice, `analyze the operating system`.

User-visible result on 0.0.45: subagent rooms show undecryptable
"Waiting for message" placeholders; gateway not interrupted by `stop`
(model answered "Stopped — ..." as a normal turn instead); spawned agents
ace/king never reply in their rooms. One minor improvement vs 0.0.44:
message rows now ARRIVE (placeholders) instead of nothing.

## Critique of the v0.0.44 analysis (what was wrong, and why)

1. "Just update" (first draft): WRONG. Misread the repo's stale checked-in
   DIST_INFO.txt (0.0.15) as the VM version. Live cat proved 0.0.44, now
   0.0.45. Lesson: never infer deployed version from repo files; read the
   target. (Corrected in the same session, but the mistake cost a cycle.)
2. E2EE framed as client-side fallout ("phone reset, re-verify"): WRONG
   DIRECTION. The dominant live error on 0.0.45 is server-side, ours:
   `live send failed — queued for retry: UNIQUE constraint failed:
   devices.user_id, devices.device_id`, repeating ~10x in the test window.
   Every send through `ensure_room_share` → mautrix `_fetch_keys` →
   `_process_fetched_keys` → our `put_devices` override can hit it, and the
   v0.0.45 retry queue then retries the same poisoned send up to 5x — log
   spam that never heals. The client-side staleness (A2SrZ7yScF invalid
   signature) is real but secondary: exactly ONE device, already excluded
   by the claim-verify guard; shares SUCCEED around it.
3. Followup/steer/discovery/spawn fixes treated the Matrix lane as the whole
   system. The 0.0.45 traffic proves the engines work (cow files built, ace
   session row created, king RPC proc alive PID 17081) and the rooms exist —
   the break is (a) sends die in OUR crypto store write path, (b) owner
   trust never reaches VERIFIED so shares exclude/rotate forever, (c) the
   retry added in v0.0.45 amplifies (a) instead of healing it.
4. The probes' file:line maps were accurate; their verdicts ("fixed at HEAD,
   just deploy") were not, because no probe ran the LIVE send path against
   the real store. Green unit suites with fake executors cannot catch a
   UNIQUE-constraint race inside the mautrix fetch→store callback.

## Finding A — every send can die in OUR put_devices (PROVED, dominant)

Log (sidecar.log, 20:29:58–20:34:23, ~10 occurrences):

  observatory.renderer: live send failed — queued for retry:
    UNIQUE constraint failed: devices.user_id, devices.device_id

Mechanism (`hermes/observatory/e2ee.py`): our `SQLiteCryptoStore.put_devices`
(:1037-1048) calls `super().put_devices()` (memory replace — fine), then
`DELETE FROM devices WHERE user_id=?` + multi-row `INSERT`. mautrix
`_process_fetched_keys` (`device_lists.py:62-104`) wraps the whole fetch in
`async with self.crypto_store.transaction()` — and our store inherits the
abstract NO-OP transaction (`abstract.py:92-96`: "If the store doesn't support
transactions, this can be a no-op" — just `yield`). So concurrent
ensure_room_share calls interleave: A DELETES owner's rows, B DELETES,
A INSERTs, B INSERTs → second INSERT hits the (user_id, device_id) PRIMARY
KEY (`e2ee.py:701-706`). aiosqlite serializes statements on ONE connection,
but the awaits between DELETE and INSERT let two coroutines interleave
statement-by-statement. The send then fails, the v0.0.45 retry queue replays
it up to SEND_RETRY_MAX=5 times, each retry re-entering the same race.

Why it bites NOW: every Matrix send runs ensure_room_share →
ensure_owner_trust → `_fetch_keys` → put_devices. Pre-E2EE rooms never hit
this path; post-onboarding every room does, concurrently (gateway room +
child room + directives + retry ticks).

Fix direction: make the SQLite mirror write atomic — real transaction (BEGIN
IMMEDIATE + COMMIT around DELETE+INSERT, or single INSERT OR REPLACE without
the DELETE, deduping the `rows` list by (user_id, device_id) first), and/or
serialize put_devices per user with a lock. Also make the retry queue classify:
UNIQUE-constraint-class errors are poison, not transient — drop + warn, do not
retry 5x. Files: `hermes/observatory/e2ee.py:1037-1048` (+ schema :701-706),
`hermes/observatory/renderer.py` retry classifier.

## Finding B — owner trust stuck below VERIFIED, shares rotate/exclude forever (PROVED)

Crypto DBs (live read): BOTH `merc_gateway-agent.db` and
`merc_cow-site-builder.db` hold owner devices A2SrZ7yScF + W99WULhUjW with
`trust=0` (UNVERIFIED) — nothing ever reaches VERIFIED (2).

Mechanism: mautrix `_validate_device` resets every refetched device to
UNVERIFIED; our `ensure_owner_trust` (`e2ee.py:1760+`) is supposed to
TOFU-verify after the last fetch via `trust_device_tofu` → `put_device`
(:1050-1064). But `trust_device_tofu` reads back via `store.get_device`
— which our SQLite store DOES NOT OVERRIDE, so it hits the MEMORY dict.
Whenever the in-memory dict and the SQLite mirror disagree (and Finding A
guarantees they do — memory replaced, SQLite write failed), the read-modify-
write loses: either it re-verifies a stale object, or `existing` misses and a
second `put_device` races. Net: trust=0 persists across restarts (SQLite is
the restart source of truth, :842-851 loads trust from the db).

Second half: device A2SrZ7yScF's OTKs fail signature verification
(mautrix `encrypt_olm.py:90`: `Invalid signature for A2SrZ7yScF of @owner:vm`,
20:29:12 — the genuine identity-reset shape; our claim-verify guard
(`verify_recipient_otks`, :1922+) correctly excludes it). So every share =
TOFU attempt (fails to persist, Finding A) + exclude A2SrZ7yScF + share to
W99WULhUjW only + rotate on next trust-change signal. The phone, holding
Megolm sessions addressed to a different share set, shows "Waiting for
message" for everything — exactly the reported symptom. The sends technically
"work" (shares succeed: `Group session ... successfully shared` throughout),
but the trust/exclusion churn means the owner's client can never catch up.

Fix direction: (1) fix A first (it corrupts the trust persistence this needs);
(2) override `get_device`/`get_devices` to read the SQLite mirror (or keep
memory+SQLite in one atomic write so they cannot disagree); (3) verify
`trust_device_tofu` persists VERIFIED across a restart in a test that kills
the store object between write and read; (4) surface the A2SrZ7yScF rotation
as the existing verify-notice (it may already post — confirm the owner sees
the `trust-device` command) rather than silent exclusion.

## Finding C — gateway `stop` is answered, not obeyed (PROVED, mechanism)

Transcript (hermes state.db, session `gateway`, ids 2947-2970): user `stop`
at 20:33:38 → model ran process-list + delegate-list + todo + cron-list as a
NORMAL turn → "Stopped — no background processes, subagents, or cron jobs
running." Same at 20:34:08. The `stop` text never touched the busy/interrupt
path — it became turn content because the gateway was IDLE (cow child already
done 20:30, nothing running). `/stop`-as-word is a model decision, not an
interrupt; the v0.0.45 steer work only covers the live-turn window.

Two gaps: (a) no `stop`/`cancel` COMMAND exists in the gateway slash surface
that kills background batches when idle (midflight-steering doc already notes
`/stop` doesn't touch background batches; `interrupt_for_session` callers are
only reset paths); (b) Matrix room `stop` routes as InjectText kind=steer
(`control.py:608-621`) — with nothing live, the steer-miss path now (v0.0.45)
runs it as a fresh turn, i.e. asks the model to "stop", which narrates. CLI
parity claim in the brief was overstated: CLI `stop` during an IDLE gateway
would do the same; the real CLI behavior users remember is Ctrl-C / busy
interrupt, which Matrix has no equivalent for.

Fix direction: add a real terminal verb — e.g. `/stop` handled as
`interrupt_for_session` + batch abort for the caller's session in BOTH idle
and busy windows (native + Matrix router), with an ack that names what died
(or "nothing was running"). Do NOT route bare `stop` text to the model when a
steer-miss + no-live-turn + batch-exists condition holds — or at minimum make
the miss path check `has_live_for_session` and answer deterministically.

## Finding D — spawned agents ace/king are correctly created AND correctly refused (no bug, but silent)

ace (hermes, session_ref `20260912_202808_548740`): NO session row exists.
king (omp, session_ref `.../2026-09-13T00-28-45-758Z_....jsonl`): FILE MISSING
(omp-sessions/ holds only a Sep-11 file), yet king's RPC proc is ALIVE (PID
17081). Both `session_materialized=false`, both live depth-0, rooms+spaces
converged, MXIDs minted. The v0.0.45 orphan-marker SHOULD have posted
CHILD_ORPHAN_NOTICE at the 20:25 boot — sidecar.log shows NO orphan line, NO
spawn line for either node. So both were spawned AFTER the 20:25 boot (ace
session id timestamp 20:28:08) through the gateway slash path
(`_handle_observatory_spawn`), which registers the handle in-process;
the sidecar's `_adopt_live_spawn_handle` should adopt it — but any message in
those rooms hits `_child_handle` → miss → "session is unavailable", and the
rooms stay silent because of Findings A/B (even the notice/error sends die).

Fix direction: trace the gateway→sidecar handle handoff for post-boot spawns
(`platform_hook.LAST_BOOT.registry` vs sidecar's adopted registry object —
the adopt happens once at boot; comment at `sidecar_main.py:2746-2752` admits
a LATER boot leaves the daemon looking at a different object). Likely the two
registries diverged across the 20:23 SIGTERM → 20:25 restart. Make the adopt
lazy-per-miss (already partially there — verify it actually fires), and make
"no handle" resolve by RESUMING the hermes session / REATTACHING the live omp
RPC (king's proc is alive — find it by session_dir, don't orphan it), with the
orphan notice as last resort, posted LOUDLY (it never appeared in logs).

## Fix plan (proposal — dispatch on approval)

1. E2EE store atomicity (P0, blocks everything): atomic put_devices
   (INSERT OR REPLACE, no DELETE gap; real transaction; per-user lock) +
   retry-queue poison classifier. Red test: concurrent put_devices same user
   → assert exactly N rows, no UNIQUE failure; retry test: UNIQUE-class error
   → dropped once with warning, not 5 retries.
2. Trust persistence (P1, after 1): SQLite-backed get_device/get_devices (or
   atomic mirror writes) + restart-surviving VERIFIED test + confirm
   verify-notice surfaces A2SrZ7yScF rotation to the owner.
3. Real stop verb (P1): idle+busy `/stop` → interrupt_for_session + batch
   abort + deterministic ack; Matrix router maps bare `stop` to it when no
   live turn but batches exist. Red test: idle gateway + live batch + `stop`
   → batch dead + ack names it (never a model narration).
4. Spawn handoff (P1): lazy registry re-adopt on every `_child_handle` miss +
   reattach live omp RPC by session_dir + hermes resume by session id; orphan
   notice only when truly nothing exists, and LOUD (log + room).
5. VM ops (no code): owner phone force-quit/rejoin + verify A2SrZ7yScF or
   approve rotation via trust-device; `/exit` the dead ace/king nodes after
   the fixes so fresh spawns prove the path.

## Verification performed (all read-only)

- VM DIST_INFO 0.0.45 / source 4cdaacec; procs 15678/15703/16948 from 20:25.
- Observatories state: gw + ace + king live; child-room crypt entries
  (incl. deleg_97b18adf/0 → !rlYVThQhNTLPaBHTID); cow-site files exist.
- deleg_97b18adf completed/pending/attempts=1 (NEW 0.0.45 traffic, still
  pending — native lane still drops Matrix completions, unchanged).
- Sidecar 20:25–20:36: followup fix WORKED once (`queued after busy gateway`
  for the cow child); sends then died on UNIQUE-constraint (~10x); shares
  succeed around excluded A2SrZ7yScF; mass decrypt failures continue.
- Gateway transcript 2940-2990: cow dispatch → analysis turn → stop answered
  as narration (twice) → followup inject verified files in-chat (the ONE real
  improvement: parent DID continue via the new wait-then-inject path).
- Crypto DBs: both machines, owner trust=0 for both devices (live read).
- No repo files changed by this analysis except this doc (uncommitted); no VM
  files changed (reads only via sudo -u user + sqlite selects).
