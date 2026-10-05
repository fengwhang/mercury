# Integration audit — 2026-10-04

## Result

The audited integration worktree is being prepared for the authorized
**v0.4.0-nightly** experimental release. The reproduced package-manager,
connector, browser, model-routing and voice-transport blockers are fixed.
The stable v0.3.24 room-cleanup fixes are included, including the legacy
misregistered-delegate migration.

Audit repairs and release metadata are committed on `integration/parity-voice`
before building: the distribution packer archives committed source. Stable
`main` remains at v0.3.24 (`c29511ab`); the shared development checkout and
installed Mercury services are untouched. The limits and historical audit
results below remain applicable. This release is not a claim that the full
Hermes test suite or real-device voice acceptance is complete.

## Merge provenance

- Worktree: `~/Documents/mercury-worktrees/integration`.
- Branch: `integration/parity-voice`.
- Draft before merge: `98a8c053f55258aa0a661c8e330aae786709ec10`.
- Shipped source: `61e1683afa41197daeae2ba0bfb4535ee55ca41d`
  (`Release Mercury v0.3.23 stable`).
- Merge: `c2d79bc95c2848f55241f750e469c682823dce76`.
- Merge completed without conflicts. The shipped source is an ancestor of
  integration HEAD.
- During the original audit, local main stayed at
  `bbf8a755189ce9e72d16099b253a43212c592e70`.
  The shared checkout stayed on `fix/provider-auth-sync` at `97c54ef9`, clean.

Stable v0.3.24 (`c29511abddedf32c3cda9494d9f809d8c8e64fde`) was subsequently
merged into integration (`f8c15a00`), preserving the audited repairs.

The original integration diff against v0.3.23 spans 783 files and approximately
77,000 added lines. A large part consists of extracted stock-Hermes helpers;
their presence is not proof their features were integrated into Mercury.

## Issues fixed during this audit

### Voice transport and browser recording

- Added a distinct sidecar-to-MIRC service credential for the protected
  voice/status/audio APIs. It grants no configuration/admin access and never
  goes to the browser. Browser-to-sidecar authentication remains separate.
- Resolve room engines from the read-only Observatory state database when
  the web server and gateway are different processes. Unknown/expired rooms
  are refused. A caller-provided engine hint cannot bypass the Hermes guard.
- Corrected the WebSocket upgrade to HTTP/1.1 and preserved bytes already
  buffered by the HTTP handler. Enforced client masking, frame/control rules
  and the aggregate message-size limit.
- STT and TTS run on bounded worker queues so a slow provider does not block
  the socket's ping/hangup handling. Late results after hangup are discarded;
  STT errors are shown to the caller.
- Record independent audio containers instead of treating MediaRecorder
  timeslices as complete files. Signal the browser's actual container,
  including MP4 for browsers that select it.
- Stop a microphone acquired after cancellation and ignore obsolete socket
  callbacks. Speak only messages tagged `assistant_reply`, including replies
  arriving together or when the message buffer length stays constant.
- Updated `docs/voice-call.md` and the secret placeholders with authentication,
  HTTPS/WebSocket requirements, and the remaining acceptance limits.

### Model selection and CLI flags

- Semantic search previously selected from the entire credentialed model
  pool. It now uses the active task model or configured delegate model and
  only the user's declared fallback chain. Missing/invalid configuration
  errors instead of selecting an unrelated model/provider.
- Judgment usage is attributed to Mercury's sole OMP model role, `task`.
  No additional model roles were introduced.
- Restored `--no-prewalk` to the CLI argument-hoisting table so it does not
  leak into unrelated subcommands.

### Config, connectors, vault and browser extraction

- Plugin selection/eviction now operates on `hermes` in a unified Mercury
  config, while preserving `models`, `profile_models` and `omp`. Legacy
  engine-only profile files still work. Corrupt YAML is rejected correctly.
- Replaced unavailable YAML-wrapper imports with the installed YAML runtime.
- Connector availability now reads the existing Nous account claim. The
  connector client uses Mercury's existing gateway origin and refresh-aware
  auth, which refuses to attach the token to a different origin.
- Connector update events now use Mercury's home identity helper and reach
  only the owning profile/session. Mixed local/connector batches return a
  useful error instead of importing an absent validation module.
- Vault unlock and payment confirmation use the existing approval harness;
  removed an unsupported consent argument. Ordinary local browser sessions
  work without the unshipped Bot Desktop package; errors inside an installed
  desktop backend are not swallowed.
- Extracted browser sandbox and process-cleanup helpers preserve facade
  overrides. Updated two test fixtures so they exercise real security imports
  and command timeout recovery instead of failing at an unrelated preflight.

### Managed dependencies and packaged installs

- Exposed `mercury pm` through the public CLI and replaced broken stock command
  hints with Mercury commands. The separate manager runtime now has its required
  manifest, launcher, generation lease, publication journal and recovery APIs.
- Made plugin/config discovery follow Mercury's actual layout: default engine
  plugins under `hermes/plugins`, named profiles under `hermes/profiles`, and
  default settings in the central `config.yaml`. Shared models and OMP settings
  survive plugin selection and eviction.
- Removed application-only YAML/auth imports from the private PM worker. It
  uses its own locked dependencies and can read the same lazy-install policy as
  the caller without importing the application.
- Separated PM's pinned Python 3.14 from Mercury's application interpreter.
  Application generations use the installer's supported Python (currently
  `>=3.11,<3.14`). Both paths were exercised using the real pinned downloader.
- New launches select committed generations; running processes retain leases.
  A changed project manifest, lock, interpreter or plugin selection invalidates
  the generation. A stale generation uses the installer runtime and tells the
  user how to refresh it. Explicit `MERCURY_PYTHON` overrides remain supported.
- Included PM modules, its private lock/metadata, and Observatory modules in
  wheel/sdist builds. Kept Mercury's own tarball builder; removed the unusable
  stock native-bundle CLI command.

### Connection-card completion and failure handling

- Adapted MCP cards to Mercury's existing MCP/config/OAuth implementation.
  Probe in memory before saving; store declared secrets in `.env`, with
  nonsecret setup values inline in the server configuration.
- Configuration and credential commits share the dashboard mutation lock.
  Credential-write and OAuth-commit failures restore the previous files and
  touched environment keys. Failed/cancelled authorization restores prior token
  state; canceled workers cannot commit late.
- Bound live connection operations and OAuth storage to the initiating profile.
  Adapted callback receivers and polling to the existing TUI flow registry.
- Catalog installs use shipped reviewed pins, the existing security scanner,
  and atomic source metadata publication. Installed catalog entries are now
  reported as installed. Failed plugin installs do not persist submitted secrets.
- Status copy distinguishes installation from confirmed live activation.
  Plugins declaring additional Python dependencies may require
  `mercury pm install venv` and a new session/Observatory restart. This port does
  not promise automatic activation in every already-running chat.

## Scope and remaining limitations

**Voice remains experimental and opt-in.** The UI labels it experimental and
requires an explicitly configured sidecar. Audio containers, speech-pause
segmentation, barge-in invalidation, HTTP/WebSocket protocol, authentication,
worker queues and teardown have automated coverage. Real microphone quality,
Safari/iOS audio playback, acoustic echo handling and live provider latency
remain hardware/browser acceptance work. Ordinary text chat does not depend on
voice. See [voice-call.md](voice-call.md).

**The imported helper inventory is not a claim of complete stock parity.**
The core `run_agent.py` and `gateway/run.py` loops retain Mercury's existing
implementation. The updated [missing-import inventory](integration-audit-missing-imports.json)
records 38 remaining local module references: four potentially reachable
optional render/backend imports have explicit fallbacks; the rest occur in
extracted helpers not wired into the reviewed Mercury entrypoints. This is a
static import-graph assessment, not a proof about arbitrary third-party plugin
imports. Do not wire those helpers into core execution without completing their
contracts. Other intentionally deferred OMP surfaces are documented in
[omp-predict-credits-deferred.md](omp-predict-credits-deferred.md).

**Platform acceptance has limits.** All four Linux artifacts were built and
passed version/native-sentinel/ELF portability checks. The x64 GNU artifact was
installed and run on this NixOS host. ARM64 and musl artifacts were not run on
physical target hosts; macOS-specific signing tests were skipped on Linux.

## Validation evidence

All Python tests used `hermes/scripts/run_tests.sh` and an isolated review
venv. Cached Node/native dependencies were staged as ignored worktree links;
workspace aliases point at integration source. No live provider turn or
installed gateway was needed.

| Check | Result |
|---|---|
| Observatory/RPC/STT/registry regressions | 463 passed; large TUI file rerun separately |
| Large TUI gateway regression file | 626 passed |
| Final family/rooms/progress/fleet/permission regressions | 86 passed across 10 files |
| Final adapted browser/compression/TTS/MCP/runtime paths | 90 passed across 6 files |
| PM lifecycle artifacts and publication/config tests | 45 passed; 1 macOS-only skip |
| Private PM worker under Mercury's Python bounds | 4 passed; real upgrade, failure preservation, policy, staleness |
| Wheel/sdist guards and packaged module/assets checks | 4 passed |
| Config/update/MCP compatibility checks | 115 passed across 7 files |
| Real MCP stdio/card/OAuth and TUI OAuth contracts | Passed, including probe and disk-write rollback |
| Catalog pinned Git install, status and failure credentials | Passed |
| OMP release/storage regressions | 137 passed across 10 files |
| OMP command/retry/recovery/judgment regressions | 129 passed across 9 files |
| OMP supported type checker and native builds | Passed |
| Native OMP synthetic local-provider tool turns | yolo: 0 prompts; write: 1; always-ask: 2; all shell/write/read actions completed |
| mLounge full suite | 377 passed across 46 files |
| mLounge production build, changed-file ESLint and Prettier | Passed |
| Hermes TUI production build | Passed |
| Real pinned PM bootstrap and core application generation | Passed; full Mercury imports and CLI version on Python 3.13 |
| Four Linux candidate archives and checksums | Built; version, native sentinel and portable ELF gates passed |
| Packaged installer phases in a temporary home | Checksum, unpack, venv, both engine smoke tests, defaults and profile list passed |
| Installed launcher's committed-generation selection | Passed on installer Python 3.11; next launch selected the generation |
| Whitespace and changed shell syntax | Passed |

The original audit candidate packaging used an external Git index and a read-only Git adapter to
archive the reviewed working-tree snapshot. The real index, branches and HEAD
were untouched. Candidate archives retain `0.3.23` only for local smoke testing;
they are **not published releases**. The v0.4.0-nightly release preparation
uses the normal committed-source packer and fresh builds of all four targets,
rather than those local candidates. Follow [CONTRIBUTING.md](../CONTRIBUTING.md).

Installer smoke testing invoked the real fetch, dependency, engine and default
seeding phases. Public PATH/shim modification, service setup and service startup
were omitted to protect the operator's installation. The PM doctor correctly
reports unprovisioned optional/required tools in the deliberately partial test
store; that diagnostic is not claimed as an all-tools install success.

The broad Hermes run covered 2,760 files: **31,667 passed, 263 failed,
314 skipped**, plus 11 files with no collected/completed tests and one
nonzero exit after passing tests. This is not a green suite. Failed-file
comparisons ran against the exact v0.3.23 source in a separate worktree.
Most failures reproduce there, including stock tests for intentionally
removed child engines/kanban, unavailable optional packages and Nix-host
assumptions. Three additional timeout failures passed on focused rerun.
The new browser extraction regressions were fixed and rechecked.

The imported OMP vision-role assertion was corrected to verify that the legacy
role is ignored. Mercury still has only the `task` model role. Outstanding
baseline failures remain separate triage work; they are not hidden by this
nightly assessment.

Local detailed logs from this run are under `/tmp/mercury-integration-*.log`.
They are temporary evidence, not distribution assets. The integration is ready
for the nightly preparation workflow with the scope and acceptance limits above.

## v0.4.0-nightly preparation

The authorized release preparation on the integration branch includes the
v0.3.24 stable fixes and the audit repairs above. Focused final checks passed:
105 Python tests across 13 files (one macOS-only signing test skipped),
73 OMP tests, the supported OMP type checker, and all 378 mLounge tests across
46 files. The Python checks cover managed runtime publication, connector/card
rollback, voice transport, room expiry/restart, family trees and packaging.
The final release process rebuilds both interfaces and four Linux OMP targets,
then verifies committed source, versions, portable ELF headers and checksums
before publishing. ARM64 is cross-built and inspected, without a hardware run.
This nightly leaves stable/main at v0.3.24 and does not deploy or restart
installed services.
