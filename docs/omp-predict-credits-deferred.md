# Deferred OMP surfaces: predict + credit-layer session features

Why four stock OMP surfaces stay unported on the Mercury integration
branch. Each cites the exact stock file that blocks it and the hard law
it would violate. Revisit only under the stated conditions.

## `omp predict` — deferred (TUI surface + runtime weights download)

Stock surface: `packages/coding-agent/src/commands/predict.ts` (oclif
`Predict`, TTY-gated: throws unless stdin/stdout are TTY) drives
`src/cli/predict-cli.ts:runPredictCompare`, a full-screen multi-lane
ghost-text comparison built on `@oh-my-pi/pi-tui` (`TUI`,
`ProcessTerminal`, `native/describe`, `prompt/word-completion`).
The `smollm` lane requires a ~147 MB runtime download:
`src/predict/smollm-weights.ts` pins `HuggingFaceTB/SmolLM2-135M` and
`QuantFactory/SmolLM2-135M-GGUF` at fixed revisions behind
`https://huggingface.co`, fetched on first use
(`predict/client.ts`) or via `omp tiny-models download smollm`
(`predict/daemon.ts`); stock `cli/tiny-models-cli.ts` imports the
weights helpers directly. There is no omp.sh/live.omp.sh phone-home in
this tree — the blockers are (a) a TUI-native interactive surface that
cannot land without touching the frozen TUI dir, and (b) an unvendored
network-at-runtime binary fetch. Integration evidence of intent:
`src/predict/` and `src/cli/predict-cli.ts` are absent, and integration
`cli/tiny-models-cli.ts` already drops all smollm/weights imports.
Revisit only with a vendored model-distribution story AND a TUI-surface
policy; until then `tiny-models` stays title-models-only.

## Prompt-cache warmer (`session/cache-warmer.ts`) — deferred (diverged credential semantics + autonomous spend)

Stock `CacheWarmer` (`packages/coding-agent/src/session/cache-warmer.ts`,
681 lines; constructed in `sdk.ts`, driven from `agent-session.ts`)
replays the last provider request before cache expiry. Its tier decision
inspects live credentials: it reads `request.options.apiKey` and calls
`isAnthropicOAuthToken` (from `@oh-my-pi/pi-catalog/utils`) so OAuth
subscriber seats take the 1h long-cache tier. That bakes in OMP's
first-party-subscription credential model (OAuth-vs-key tiering), which
Mercury's vault/connections credential layer does not share. It is also
autonomous paid traffic gated only by a $0.05 expected-savings floor.
Absent from integration (no `CacheWarmer` wiring). Revisit only alongside
an explicit Mercury cache-retention + background-spend policy.

## Claude auto-reset (`session/claude-auto-reset.ts`) — deferred (stored-credential redemption + UI-gated auto-spend)

Stock `planClaudeResetRedemptions` (503 lines) plans redemption of
Anthropic subscription saved-resets against per-account usage reports,
keying accounts by stored credential identity
(`anthropic|orgId|credentialId`) and ranking via `claudeRankingStrategy`
from `@oh-my-pi/pi-ai/usage/claude`. It assumes OMP's `AuthStorage`
OAuth-seat inventory and `listResetCredits`/`redeemResetCredit`
plumbing. Spending is consent-gated on interactive UI
(`agent-session.ts` `#confirmAutoRedeem`: headless hosts get a notice
and NEVER spend) — a gate Mercury's headless/mLounge surfaces cannot
meaningfully offer. Integration keeps the Codex sibling
(`codex-auto-reset.ts`) but drops the Claude planner. Revisit only if
Mercury models Claude subscription seats as first-class vault identities
with an explicit auto-spend consent story.

## Anthropic slow mode (`session/` + `pi-ai/providers/`) — deferred (OAuth-subscription-only; hard-wired to `AuthStorage`)

Stock slow mode (OMP's port of `/low-priority`) continues opted-in
sessions on spare capacity past a subscription usage wall. By provider
contract it applies ONLY to first-party OAuth (`api.anthropic.com` with
a subscription bearer; every other route ignores it). The auto-accept
gate enumerates sibling OAuth seats through the credential store
(`anthropicSlowModeHasNoSiblingHeadroom` lists `authStorage.credentials`
filtering `type === "oauth"`), called per-request from `sdk.ts` via
`modelRegistry.authStorage`. Porting the controller without OMP's
multi-seat OAuth inventory yields dead code on every non-subscription
route; porting the inventory means adopting the diverged credential
layer. Fully absent from integration (both files, the pi-ai export, and
the sdk wiring). Revisit only if Mercury gains first-party Claude
subscription seats with multi-account rotation.

## Common thread

The `auth-storage.ts` re-export itself is NOT the divergence. The
divergence is the OAuth-subscription machinery above it: multi-seat
OAuth inventory + health + ResetCredit list/redeem + OAuth-vs-key tiering
in pi-ai/pi-catalog, which Mercury replaces with the hermes vault +
connections/setup toolsets. The three deferred session features all
consume that machinery; that shared dependency is why they stay out
together.
