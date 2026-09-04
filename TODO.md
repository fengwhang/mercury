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
