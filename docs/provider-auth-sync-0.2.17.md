# Provider login recovery (0.2.17)

Mercury's Hermes setup and OMP login UI previously wrote independent credential
stores. A ChatGPT login made through Hermes therefore did not appear in OMP.
OMP also advertised Codex client version `0.144.1`, which current upstream
identifies as too old to discover GPT-6.1 Sol.

The selective upstream comparison used oh-my-pi commit `2b023d1` and
hermes-agent commit `bddd22be`, fetched on 2026-09-30. This is a targeted fix,
not a wholesale upgrade of either vendored engine.

## Result

- The profile's Hermes auth store is the shared authority for ChatGPT,
  Anthropic, xAI OAuth, and providers with compatible API-key contracts.
  OMP retains its SQLite usage, selection, and backoff state.
- Both login directions work. Setup singletons are imported before the
  Hermes runtime has seeded a credential pool. Existing OMP logins migrate
  once without overwriting an already known canonical grant.
- Shared OAuth refresh runs under Hermes' existing cross-process auth lock.
  Hermes also rereads manually added Codex pool rows before spending a
  refresh token that OMP may already have rotated.
- Workspace and member identities remain separate. Logout and suppressed
  login sources cannot be resurrected by a stale SQLite mirror on restart.
- A selected Hermes profile gets its own OMP directory, including when the
  default launcher directory was inherited. Explicit SDK stores and
  independent OMP profiles do not import the operator's shared grants.
- OMP uses upstream's Codex client version `0.159.0` and versions its model
  cache namespace accordingly. Hermes probes the credential's own discovery
  route with the upstream newest-version and ungated-version fallbacks.
  GPT-6.1 Sol has provider compatibility rules; live discovery remains the
  source of available account models.

Credential interchange uses private subprocess stdin/stdout. Requests, token
responses, and provider exceptions are never included in error messages or
command-line arguments. The launcher supplies its dependency-complete Python
interpreter to OMP. External Codex/Claude credential files are not imported by
this bridge. OAuth providers with different engine contracts remain native.

## Validation

The focused Python tests cover login imports, API-key resolution by the real
Hermes pool, account boundaries, logout, suppression, serialized refresh,
Anthropic JSON refresh, manually added Codex peer refresh, and model discovery.
The Bun IPC tests run real Python subprocesses against temporary profile
stores, including the real `AuthStorage.create` factory and inherited
launcher environment. Related Codex discovery, streaming, identity, API-key
login, and directory tests were run, plus AI/catalog/utils type checks.
The focused Python tests passed (46 tests), as did all six real-subprocess
IPC tests. Both Linux binaries were rebuilt after the version bump; the x64
binary passed its complete compiled smoke test outside the restricted sandbox.
ARM64 execution is reserved for the Raspberry Pi check.

Two existing tests in `auth-storage-oauth-refresh-race.test.ts` fail identically
on the untouched `39b8813b` (v0.2.16) checkout:

- `does not disable when peer rotates between pre-check and CAS disable`
- `still disables when the failure is real (no concurrent rotation)`

They are pre-existing failures, not passing validation for this candidate.
Live provider reauthentication, inference entitlement, and Raspberry Pi
execution still require user testing. No live provider credentials were
modified during these tests.
