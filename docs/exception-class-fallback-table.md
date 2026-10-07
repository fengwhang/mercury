# Exception-class fallback table (both engines)

Scope: every provider exception class, where it routed **before** the
`agent/v045-fallback` work (`a55e9c43`), where it routes **now**, and the
gate that decides. "Chain" = `models.fallback`/`models.fallback_chain`
(Hermes, `agent._fallback_chain`) or `retry.fallbackChains` (OMP, written by
`mercury omp-sync` from the `delegate_*` slots — `bridge/bridge.py:376-387`).

Graceful contract (both engines): walk the chain in order, one activation
per entry; a notice names what happened and which model now answers
(Hermes: `⚠️ Model fallback: <old> via <provider> unavailable (<reason>);
using <new> via <provider>` — `chat_completion_helpers.py:2955-2968`; OMP:
`retry_fallback_applied{from,to}` events plus `switched model; retried`
notes); the switch is sticky for the rest of the turn; only when the whole
chain is exhausted does a terminal error reach the user (Hermes: the class
terminal after `try_activate_fallback` returns False; OMP:
`auto_retry_end{success:false}` — the event mLounge renders as
`Provider retries failed`).

Old-routing column = `a55e9c43`. New-routing column = branch HEAD. Line
numbers are at HEAD.

## Hermes (`hermes/`)

Classifiers: `agent/error_classifier.py` (`classify_api_error`,
`FailoverReason`, `is_usage_limit_exhausted`). Loop gates:
`agent/conversation_loop.py` (`is_rate_limited`, `_is_transport_failure`,
`_should_fallback`, auth-failover gate, `is_client_error`, retry-exhaust
consult). Chain walk: `agent/chat_completion_helpers.py::try_activate_fallback`.

| exception class | FailoverReason | old routing | new routing | gate | file:line |
|---|---|---|---|---|---|
| rate limit (429) | `rate_limit` / `upstream_rate_limit` (aggregator) | pool rotation → eager failover → retry burn → terminal | unchanged (already reaches chain) | `is_rate_limited` + `_should_fallback`; upstream skips pool guard | `conversation_loop.py:5560-5604, 5620-5625, 5658`; `error_classifier.py:1437-1445, 1490-1493` |
| usage/quota (plan-wide) | `billing`/`rate_limit` + `is_usage_limit_exhausted` | pool rotated keys with no chain; early abort only after chain exhaustion → retry burn with no chain | **CHANGED on branch**: pool always declines rotation for usage walls; `_is_usage_limit` forces `is_client_error` → one chain consult then terminal; eager path bypasses pool guard | `_is_usage_limit = _should_fallback and is_usage_limit_exhausted(...)` | `conversation_loop.py:5600-5603, 5620-5622, 6305-6310, 6365`; `agent_runtime_helpers.py:1124-1132`; `error_classifier.py:257-295` |
| billing/credit (402) | `billing` (retryable=F) | pool rotates immediately → eager failover → terminal w/ billing guidance | unchanged | `billing ∈ is_rate_limited`; deliberate non-retryable abort comment | `conversation_loop.py:4915-4928, 5560, 5640-5651, 6290-6322, 6365` |
| auth/entitlement (401/403) | `auth` (`auth_permanent` has no producer) | per-provider refresh (one-shot each) → pool refresh → auth-failover gate → terminal w/ guidance | unchanged | `classified.is_auth and not _retry.auth_failover_attempted` → `_try_activate_fallback(reason=...)` (once per turn); else `is_client_error` consult | `conversation_loop.py:5056-5140, 5683-5703, 6365, 6406-6451`; `agent_runtime_helpers.py:1322-1415` |
| overload (503/529) | `overloaded` (retryable=T) | backoff; eager failover after 2 failures; terminal consult | unchanged | `_is_transport_failure and retry_count >= 2` | `conversation_loop.py:5580-5599, 6558-6585` |
| server error (500/502) | `server_error` (retryable=T) | retry burn → retry-exhaust chain consult → terminal | unchanged | consult at `retry_count >= max_retries` | `conversation_loop.py:6558-6585, 6618` |
| timeout | `timeout` (retryable=T) | backoff; eager failover after 2 failures; one primary-client rebuild; terminal consult | unchanged | `_is_transport_failure`; `_TRANSIENT_TRANSPORT_ERRORS` rebuild | `conversation_loop.py:5580-5599, 6560-6579`; `agent_runtime_helpers.py:1417+` |
| connection failure | `timeout` bucket (transport exception types) | same as timeout | unchanged | same | `error_classifier.py:1292, 2117-2119` |
| context overflow (input > window) | `context_overflow` (retryable=T, should_compress=T) | compression-only; **three terminals never consulted the chain** | **CHANGED on branch**: compression-exhausted terminal, cannot-compress-further terminal, and auto-compaction-disabled terminal now consult the chain first (larger-window model may fit); restart on fallback | consult before terminal; `restart_with_rebuilt_messages` | `conversation_loop.py:5457-5485 (compaction-disabled), 6180-6197 (max attempts), 6269-6286 (cannot compress further)` |
| long-context tier (Anthropic extra-usage gate) | `long_context_tier` (retryable=T) | reduce ctx 200k + compress → falls through to generic retry → terminal consult; compaction-disabled terminal had no consult | **CHANGED on branch**: compaction-disabled terminal consults the chain (a non-tiered model survives) | tier handler falls through; disabled-guard consult | `conversation_loop.py:5501-5557, 5457-5485, 6319` |
| byte/media-budget payload rejection (413) | `payload_too_large` (+ `image_too_large`, `image_corrupt`) | byte-scored compression + image shrink/strip; **no failover** | **preserved — no failover** (a model switch cannot fix an oversized body) | `payload_too_large` in neither failover set; compaction-disabled terminal kept for this class | `conversation_loop.py:5769-5938, 5471-5485`; `agent_runtime_helpers.py` none |
| output-cap (max_tokens over provider cap) | `rate_limit`-shaped relay wrap / `format_error` | clamp `max_tokens` and retry; unparsable variant fail-fast terminal | **preserved — no failover** (deterministic request shape) | `_wrapped_output_cap_budget`, `is_output_cap_error` | `conversation_loop.py:5570-5578, 5962-6097` |
| model_not_found (404) | `model_not_found` (retryable=F, should_fallback=T) | `is_client_error` → "Try fallback before aborting" → terminal w/ hint | unchanged | consult at `is_client_error` | `conversation_loop.py:6305-6365, 5380-5397` |
| classifier refusal / content policy | `content_policy_blocked` (retryable=F) | never retried unchanged; **a fallback MAY be tried** (a different provider may not share the filter); the refusal itself is the preserved outcome when none helps | unchanged | consult at `is_client_error` + HTTP-200 refusal path | `conversation_loop.py:3735-3810, 6359-6365, 6514-6522`; `error_classifier.py:998-1013` |
| cancellation / user interrupt | not classified (orthogonal) | abort, `interrupted: True`; never failover | **preserved — no failover** | `_interrupt_requested` checks | `conversation_loop.py:5402-5433, 3628-3658` |
| deterministic request-shape 4xx (`format_error`, `invalid_encrypted_content`, `multimodal_tool_content_unsupported`, `llama_cpp_grammar_pattern`, `thinking_signature`) | per-reason | `format_error`: `is_client_error` → consult → terminal. Others: one-shot same-provider shrink/strip repair first, then generic retry → terminal consult | unchanged (same-provider repair preserved before any failover) | one-shot `_retry.*_attempted` repairs | `conversation_loop.py:5183-5322, 4977-4998, 5220-5245, 5291-5296, 6305-6365` |
| unknown catch-all | `unknown` (retryable=T) | retry burn → retry-exhaust consult → terminal | unchanged | consult at `retry_count >= max_retries` | `conversation_loop.py:6558-6585` |

## OMP (`omp/packages/`)

Classifier: `ai/src/error/flags.ts` (`AIError` flags), `rate-limit.ts`.
Error-settle gates: `coding-agent/src/session/agent-session.ts:3255-3441`
(`recordUsageLimitOutcome` → payload pre-compaction consult → Fireworks
gate → `isRetryableError` → `isHardErrorFallbackEligible` → terminal tail).
Retry/fallback engine: `coding-agent/src/session/turn-recovery.ts`
(`#handleRetryableError`, `#tryRetryModelFallback`, `recordUsageLimitOutcome`).

| exception class | AIError classification | old routing | new routing | gate | file:line |
|---|---|---|---|---|---|
| rate limit (429 transient) | `Flag.Transient` | chain consulted on the FIRST failing attempt (`#handleRetryableError`); no budget burn first | unchanged | consult gate in `#handleRetryableError` | `turn-recovery.ts:2323-2357`; `agent-session.ts:3381-3385` |
| usage/quota (plan-wide) | `Flag.UsageLimit` + `matchesUsageLimitText` | `recordUsageLimitOutcome` marked+rotated the credential (plan wall treated as credential-recoverable even when the chain covers the model); explicit-quota retry step preferred the chain (`preferUsageLimitFallback`) | **CHANGED on branch**: `recordUsageLimitOutcome` declines the credential treatment when the limit is plan-wide AND `isHardErrorFallbackEligible` (chain covers) — the chain answers first; opaque limits and chain-less models keep rotation | `matchesUsageLimitText && isHardErrorFallbackEligible` | `turn-recovery.ts:592-627, 2207-2211` |
| billing/credit (opaque 402) | `Flag.UsageLimit` (opaque) | credential rotation first; chain only when siblings exhausted (tested contract) | unchanged — per-account balances can differ, rotation stays | `preferUsageLimitFallback` false for opaque bodies | `turn-recovery.ts:2203-2283`; tests `agent-session-retry-fallback.test.ts:2363-2575` |
| auth/entitlement (401/403) | `Flag.AuthFailed` (hard) / `Flag.AccountPolicy` (retryable) | AuthFailed → hard-error chain consult (no AuthFailed veto); AccountPolicy → `rotateSessionCredential` + chain | unchanged | `isHardErrorFallbackEligible` (3397 path); accountPolicy branch | `turn-recovery.ts:2285-2292, 1959-1987`; `agent-session.ts:3397-3410` |
| overload (503/529) | `Flag.Transient` | chain on first failing attempt | unchanged | same as rate limit | `flags.ts:160, 567-580` |
| server error (500/502) | `Flag.Transient` | chain on first failing attempt | unchanged | same | `flags.ts:160, 579-580` |
| timeout | `Flag.Transient\|Flag.Timeout` | chain on first failing attempt | unchanged | `AIError.retriable` | `flags.ts:155, 484, 539-540` |
| connection failure | `Flag.Transient` | chain on first failing attempt | unchanged | — | `flags.ts:159-160, 395-401` |
| context overflow (usage-backed) | `Flag.ContextOverflow` | compaction owns it; chain vetoed | **preserved — no failover** (compaction's job) | `isTextAmbiguousContextOverflow` waiver only | `turn-recovery.ts:1226-1228, 1976-1980` |
| byte/media-budget payload rejection (413) | `Flag.PayloadRejected` | **chain-consulted by deliberate design** (pre-compaction consult + hard-error gate; `#9235`); only the Fireworks Fast→base degrade is vetoed | unchanged (as-built divergence from the presumed "must not fail over" list — deliberate, documented, tested) | `isHardErrorFallbackEligible` has no PayloadRejected veto | `agent-session.ts:3306-3318, 3397`; `turn-recovery.ts:1945` |
| payload/context rejection a different model survives (text-ambiguous 413, strict-tool Grammar 400, FastModeUnsupported) | dual-flagged / `Flag.Grammar` | hard-error chain consult | unchanged | overflow veto waived for text-ambiguous | `turn-recovery.ts:1952-1957, 1976-1980`; `flags.ts:251-267` |
| model-not-found / router error | raw status fallback | Fireworks Fast→base degrade; else hard-error chain consult | unchanged | `isFireworksFastFallbackEligible` / hard-error gate | `turn-recovery.ts:1921-1947` |
| classifier refusal | `isClassifierRefusal` (stopDetails refusal/sensitive) | retryable path with `pinFallback: true` (a different model may answer; the turn is preserved) — bounded by retry budget | unchanged (as-built divergence from the presumed "must not fail over" list — deliberate, tested) | consult gate; hard-error path refuses refusals | `turn-recovery.ts:2331-2342, 2386-2413`; tests `agent-session-retry-fallback.test.ts:3203` |
| cancellation / user interrupt | `Flag.Abort` / `Flag.UserInterrupt` / `Flag.SilentAbort` | settles before every fallback gate; reasonless aborts retry same model with `allowModelFallback: false` | **preserved — no failover** | veto at `isHardErrorFallbackEligible:1975`; settle early return | `agent-session.ts:3347-3368`; `turn-recovery.ts:2154` |
| immutable Anthropic thinking 400 | pattern veto | terminal tail, no consult | **preserved — no failover** (deterministic request shape) | `IMMUTABLE_ANTHROPIC_THINKING_ERROR_PATTERN` | `turn-recovery.ts:88-89, 1217-1223, 1964-1970` |
| usage-preflight-blocked (fail-closed reserve) | `Usage preflight blocked:` message | terminal tail at all gates | **preserved — no failover** (user policy) | vetoes at 1215, 1932, 1961 | `turn-recovery.ts:1169-1171` |
| ThinkingLoop | `Flag.ThinkingLoop` | same-model resample w/ hidden redirect; chain suppressed | **preserved — no failover** (issue #8760) | `!thinkingLoop` consult gate | `turn-recovery.ts:2297-2300` |
| unknown catch-all | raw status / Transient | chain on retryable path, hard-error consult otherwise | unchanged | — | `agent-session.ts:3381-3410` |

## What the branch changed (routing deltas)

Hermes (branch commits + this series):
1. `recover_with_credential_pool` never rotates keys for plan-wide usage
   walls (`agent_runtime_helpers.py:1124-1132`).
2. `_is_usage_limit` forces the terminal gate so a usage wall gets exactly
   one chain walk and then stops (`conversation_loop.py:6305-6310`).
3. `switch_model` no longer prunes the user's fallback chain on
   cross-provider primary switches (`agent_runtime_helpers.py:3434-3441`).
4. Startup uses the canonical chain activation and keeps the preferred
   selection for later restore (`agent_init.py:1153-1168, 1424-1437`;
   `agent_runtime_helpers.py:1659-1701`), and an inferred startup provider
   never auto-discovers on restore (`agent_runtime_helpers.py:1665-1696`).
5. Overflow terminals consult the chain before stopping
   (`conversation_loop.py:5457-5485, 6180-6197, 6269-6286`).

OMP (this series, on top of pre-base fixes `9c9be0d4`, `e22da290`,
`8b0c8f98`):
1. `recordUsageLimitOutcome` declines the credential-recoverable treatment
   for chain-covered plan-wide usage limits (`turn-recovery.ts:592-607`).
2. The last-resort chain consult runs past a pending sibling-credential
   wait once the retry budget is spent (`turn-recovery.ts:2334-2346`), so
   the retryable path can no longer dead-end as
   `auto_retry_end{success:false}` with `retry.fallbackChains` unconsulted.

## Preserved — deliberately does NOT fail over

- Cancellations / user interrupts / silent aborts (both engines).
- Usage-backed context overflows — compaction owns them (OMP) / compression
  first (Hermes).
- Byte/media-budget 413 payload rejections in Hermes (compression/shrink
  only) — OMP consults the chain for them by deliberate `#9235` design.
- Output-cap (max_tokens over cap) and immutable-Anthropic-thinking 400s —
  deterministic request shape.
- Same-provider one-shot repairs run BEFORE any failover in Hermes
  (invalid-encrypted-content, multimodal tool content, llama.cpp grammar,
  thinking signature).
- ThinkingLoop same-model resamples (OMP, issue #8760).
- Usage-preflight fail-closed reserve policy (OMP).
- Classifier refusals: both engines may consult the chain by deliberate
  design (OMP pins the fallback; Hermes tries one), but the refusal itself
  is the preserved outcome when no chain entry helps — they are never
  retried unchanged on the same model.
