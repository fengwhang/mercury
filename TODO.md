# Mercury — post-ship TODO

Truth doc for the Mercury agent runs. Pre-ship history (tracks A–F,
NB-1..6, full evidence) lives in the archive: `~/Documents/mercury-og/TODO.md`
+ `NONBLOCKERS.md`. This file tracks what remains AFTER v0.0.1 shipped
(2026-09-05, tag + GitHub release + tarball asset live).

Legend: [ ] todo · [~] partial · [x] done

## Shipped state (context, not tasks)

- v0.0.1: history squashed, repo public at github.com/fengwhang/mercury,
  release v0.0.1 + mercury-0.0.1.tar.gz asset. install.sh one-command
  installer (uv venv from vendored pins, prebuilt omp binary, four-slot
  wizard fail-hard, approvals.mode incl. permanent off).
- One config: ~/.mercury/config.yaml (models: four slots; hermes:/omp:
  subtrees; approvals: mode). Bridge v3 (delegate_model/delegate_fallback).
- modelRoles KILLED in the fork. Delegation engine = omp. B2 kanban
  removal complete. Unified approvals (manual|smart|off) drive both engines.
- C2 deny-translation live (hermes deny globs → omp bash.patterns).

## Track C — approval routing (critical path)

- [x] **C1 slice 1** (455f82df): omp RPC transport + approval routing into
      hermes guards (select-frame parsing, dedicated responder thread,
      thread-local callback copy). Fake-server 9/9 + LIVE approve/deny.
- [x] **C1 slice 2(a)** (0c8d3812, 2026-09-05): `_run_omp_task` prefers the
      RPC transport — approval gates route into hermes guards on the
      DEFAULT delegation path. `-p` one-shot is fallback ONLY on
      `OmpRpcStartError` (raised before any prompt is sent → no
      double-execution hazard). Knobs: `HERMES_OMP_TRANSPORT=oneshot`
      kill-switch, `HERMES_OMP_RPC_STARTUP` (ready-frame probe, 20s
      default). Entries stamped `transport: rpc|oneshot-fallback`.
      Fixed post-ship casualty: `_config_path()` pointed at the removed
      repo-root config.yaml (mtime cache key permanently None → config
      edits never invalidated the delegate-env cache); now mirrors
      bridge.py's resolution chain. Tests 33/33 across
      test_omp_delegation + test_omp_rpc_transport; LIVE
      MERCURY-C1S2-OK (engine path, real patched binary + glm-5.3,
      approval gate APPROVED `sed -n '1p' probe.txt`, 6.43s).
- [x] **C1 slice 2(b)** (fc0d6c89, 2026-09-05): cron omp_direct is
      RPC-first — new helper `cron/omp_direct_rpc.omp_direct_rpc_attempt`
      wraps `tools/omp_rpc_transport.run_omp_task_rpc` and returns the
      scheduler 4-tuple; scheduler's omp_direct block calls it before the
      `-p` one-shot. Returns None ONLY on pre-prompt start failure
      (kill-switch / transport import / no ready frame) → one-shot
      fallback, no double-execution hazard; a returned tuple is final.
      `mercury omp` (TUI) examined and deliberately EXCLUDED: it
      `os.execve`s omp's interactive TUI — approvals are attended by
      omp's own UI, no headless fail-closed hazard exists there.
      Tests: tests/cron/test_omp_direct_rpc.py 6/6 (fake-server E2E via
      real vendored omp_rpc client, approve/deny gate routing,
      kill-switch, start-failure→None, SILENT parity); 39/39 across the
      three C-track suites; bridge 26/26. LIVE: real patched omp +
      zai/glm-5.3 over RPC, approval-gated `sed` executed, marker
      MERCURY-C1S2B-OK, tuple (True, doc, output, None).
- [ ] **C3**: nested-subagent check — verify a subagent-of-subagent exec
      approval surfaces through the RPC channel on a real fan-out.

## Track D — install/migration

- [ ] **D3a. Migration from existing Hermes installs.** Installer detects
      ~/.hermes (and $HERMES_HOME) and offers to port: skills, config
      (map delegation.* slots → four-slot model config; flag the rest for
      review), sessions DB, profiles; audit what else lives in a real
      ~/.hermes (memory/, plugins/, cron, .env secrets — propose list,
      ask per-category, NEVER auto-copy secrets). Migrate-by-copy (dual
      install safe), idempotent re-runs, version-stamped migration
      manifest in the mercury home. Imports INTO the MERCURY_HOME layout
      (skills/ top-level shared, hermes/ + omp/ private), not
      ~/.hermes-compatible shapes.
- [ ] D3 remainder: full clean-VM proof of the tarball install (installer
      rework in flight — see working tree).

## Track E — upstream discipline

- [ ] E1: patch-series re-pin procedure documented (clone new tag → apply
      series → bridge tests + /omp smoke → commit).
- [ ] **E3 (LAST, user decision)**: amend vendored docs (hermes/docs/,
      omp/docs/) to match the distribution: delegation=omp, unified config,
      /omp command, MERCURY_HOME layout, roles-do-not-exist, HERMES.md /
      OMP.md per-side defaults. Only after D3/D3a settle.

## NB-6 — hard-rename remainder (non-python surfaces)

- [ ] NB-6a desktop electron TS tree (backend probes/argv constructors);
      NB-6b/c upstream installer scripts + tauri updater — fold into D3;
      NB-6d pyproject console-script NAME `hermes =` → decide alongside
      D3 install layout.

## Track M — matrix observatory (SHIPPED: sidecar live since v0.0.16, main-line through v0.0.25)

Truth doc: `docs/design/matrix-observatory.md` (§3 layout locked at
`build_plan` gw-parity v0.0.25 + `render_live.py` gate asserts — no drift).
Code: `hermes/observatory/`. Midflight-steering counterpart:
`docs/design/midflight-steering.md`.
- [~] **M0/M1** (22acd41f): hermes delegation control plane (name schema,
      steer/stop forwarding, RPC methods, names persistence). Code in tree;
      suite proof: test_omp_delegation + test_omp_rpc_transport fully green
      in the final-sweep delegation run below.
- [~] **M2–M6** (ac974016): observatory package + gw-parity +
      e2ee-default-true code-side. Code in tree; suite proof: observatory
      597 green below; LIVE gates further below open.
- [x] **M-tests** (final-sweep 2026-09-08): hermes tests/observatory 597
      passed, 1 skipped, 4 xpassed; omp rpc-subagent-control bun 11/11;
      tsgo --noEmit clean (exit 0); bridge script 2 FAILED =
      {missing-fallback exit 1, missing-delegate_fallback exit 1} (known
      pre-existing, file untouched since e952cd5d); delegation+transport
      subset 436 passed, 1 skipped, 23 failed — 11 env-missing-module
      (httpx x10, openai x1), 2 collection errors
      (mercury_tools_mcp_server absent), 10 hermes/mercury rename +
      description drift — ALL pre-existing at HEAD 3a05be10, zero caused
      by this branch (touches docs only).
- [x] **M-LIVE1**: RESOLVED v0.0.25 (`render_live.py` gate: root child
      order + gateway-subspace nesting asserts; gw-parity locked at
      `build_plan`, vm-report-7 slice 3). Per-release VM converge proof
      stays OPEN (below).
- [~] **M-LIVE2**: PARTIAL. Gateway-room steer SHIPPED v0.0.23
      (gateway-answer: control-socket `inject` verb → headless turn →
      reply; plain-text-as-prompt, steer notices skipped — no busy run
      exists). Per-child RPC steer/abort fan-out PENDING (sidecar logs
      actions in `routing_log`, does not execute —
      `sidecar_main.py:43-46`). Model-facing delegate_task steer/stop
      (M0/M1 plane) live throughout.
- [x] **M-E2EE gate**: RESOLVED. E2EE-OK 15/15 live (e2ee-gate2, merged
      pre-v0.0.16, confirmed `e555db0c`; original 2026-09-08 FAIL
      M_UNKNOWN_TOKEN root-caused via mautrix 0.21.1 contract fixes);
      hardened v0.0.23 (e2ee-keyshare: TOFU trust, warmup, verify
      notices) + post-25 (e2ee-option-msc3984: encrypt option default ON,
      MSC3984 keys/query+claim from live Olm accounts, Element X notices;
      todevice-intake: MSC-normalized crypto keys + list-shape to-device).
- [x] **M-omp-swap**: SUPERSEDED v0.0.20. Live-swap procedure replaced by
      version-gated builds: make-dist fail-hards when the omp binary bakes
      a stale Mercury version (fix-omp-version-gate `5af34098`).
- [x] **Sidecar auto-install** v0.0.19 (setup-auto-vendored: setup
      auto-installs the sidecar unit, heals URL, converges tree,
      fail-closed crypto) — LOUD on post-wipe reprovision (post-25-fixes).
- [x] **Offline boot/provision/install** v0.0.21+v0.0.22: `provision()`
      defaults offline (installed-binary trust, min-version gate);
      install.sh `--offline`; online fetch degrades to keep-usable-binary;
      vendored tuwunel 1.9.0 per-arch + vendored python-olm cp313 wheels
      (SHA256SUMS-pinned, this-arch selection).
- [x] **Gateway space nesting** v0.0.25 (vm-report-7 slice 3: gateway-agent
      subspace parity at `build_plan`; owner auto-join via owner-credential
      POST /join; `mirror_cli` gate default off).
- **Wave ledger v0.0.17→v0.0.25 (+post-25 main), every merged wave:**
  - v0.0.17: bugreport-8/8b (labels, run-order, password-env, telemetry
    hard-off, card reprint, bind-mismatch/restart offer, in-repo guide).
  - v0.0.18: vm-feedback (gateway ghost verify+repair at boot, loud on
    missing ghost; FluffyChat replaces Element X as recommended client).
  - v0.0.19: fix-sidecar-registry (optional registry param), fix-omp-keypath
    (MERCURY_HOME/.env cascade, qualify short openrouter IDs),
    fix-observatory-provision (bound URL sync, sidecar repair, E2EE gate fix),
    vendor-olm-wheel + setup-auto-vendored (fully automatic setup).
  - v0.0.20: fix-olm-arch-select (this-arch wheel, never both arches),
    fix-omp-version-gate (make-dist stale-version fail-hard).
  - v0.0.21: fix-sidecar-messaging (present-tense strings + user-output
    guard), offline-boot (provision defaults offline), fix-owner-password-leak
    (deny owner-credentials.json both engines); user AGENTS.md/HERMES.md
    imperatives committed (`ba8ee250`, `54004d03`: delegate_task routing,
    no-EOS, parallel subagents on own branches).
  - v0.0.22: install-offline-provision, vendor-tuwunel (1.9.0 per-arch),
    delegation-linear-mem (once-per-batch env, single bridge spawn, no caps —
    replaces reverted cap approach `43326796`), wave-mem-profiler (RSS,
    default off), fix-browser-select, flap-fix (no restart on unchanged
    units), rocksdb-fallocate (`rocksdb_allow_fallocate=false` + heal).
  - v0.0.23: gateway-answer (control-socket transport, setup identity
    choice), e2ee-keyshare (TOFU trust, warmup, verify notice, intake crypto
    channel), setup-reconfigure-gate.
  - v0.0.24: setup-fixes-3 (real-config gates, rotation validation, atomic
    password mirror), identity-rerun (keep-data triple, login-probe verified
    rotation).
  - v0.0.25: vm-report-7 (7 VM defects: glm fallback killed, identity
    applies, gateway space, wipe choice, mirror gate, acceptance sequence
    card-last, admin 401 self-heal, poison detect, appservice 404 accounting,
    mandatory crypto, dual bind) + post-25-fixes (silent defaults fail
    closed, wipe-first order, loud sidecar post-wipe).
  - post-25 main (unreleased): vm-round-2 (mirror prompt, owner auto-join,
    sidecar reinstall contract, crash-proof cross-signing store),
    e2ee-option-msc3984, todevice-intake, mercury-cli-stomper-probe +
    containment (`a1ae7edc`).
- **Open items (honest):** FluffyChat/Element X client quirks are
  CLIENT-SIDE (Element X: no per-device verify screen, no key-request
  gesture — notices written for that reality; reinstalls ROTATE the Olm
  identity so pre-reinstall messages are unrecoverable by design); VM
  converge proof pending PER RELEASE (clean-VM end-to-end render + steer
  each release, never assumed from unit gates).
- **Standing lessons (shipped waves):**
  - cwd-pinned git ops in delegation briefs: bare `git checkout -- .` in the
    wrong cwd reverts sibling work — every brief pins cwd and scopes git ops
    to the owned worktree (extends AGENTS.md rule 4).
  - mercury_cli full-dir quarantine: full `hermes/tests/mercury_cli/` runs
    stomp the live checkout (deleted files incl. committed tests, stray
    `tuwunel-binaries/`+`wheels/` at root) via `update_from_release` until
    containment proves out — contained by live-checkout refusal under pytest
    (`a1ae7edc`) + RED guard test; report
    `docs/design/mercury-cli-test-stomper-report.md`.
  - no-silent-defaults + fail-closed: no silent glm (any version), unknown
    `mirror_cli` → off, missing crypto stack → fail closed (never silent
    plaintext), missing power-level snapshot → read-only, changed device keys
    → refused, never silently re-trusted.
  - vendored-binary strategy: python-olm cp313 wheels per-arch under
    `hermes/observatory/wheels/` + tuwunel 1.9.0 per-arch under
    `hermes/observatory/tuwunel-binaries/` (both SHA256SUMS-pinned;
    make-dist/install.sh hash-verify and select this-arch; provision trusts
    the installed binary offline, never downgrading). Rationale: PyPI ships
    no cp313 olm wheel; fetch failures must never fail installs; virgin
    hosts provision with zero network. Manual rebuild only
    (`observatory/scripts/build_python_olm_wheel.sh`, never auto-invoked).

---

State snapshot (2026-09-05, post-C1-slice-2b, fc0d6c89): shipped v0.0.1
public; delegation engine AND cron omp_direct both RPC-first with
approval routing live end-to-end (engine → RPC child → guard stack →
verdict); `mercury omp` TUI excluded by design (attended). Next critical
path: **C3** nested-subagent approval check → **D3a** migration → **E3**
docs LAST. Run tests:
`PYTHONPATH=<repo>/hermes /opt/hermes/.venv/bin/python -m unittest
tests.tools.test_omp_delegation tests.tools.test_omp_rpc_transport
tests.cron.test_omp_direct_rpc` (39) + `python3 bridge/test_bridge.py`
(26).
