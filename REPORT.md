# mercury_cli test stomper — probe report

Branch: `agent/mercury-cli-stomper-probe` off `main` @ `55bf9fae`.
Scope: INVESTIGATION ONLY. No product code changed.
Note: the brief cites `hermes/tests/test_checkout_mutation_guards.py`; that path
does not exist. The real guard file is
`hermes/tests/mercury_cli/test_checkout_mutation_guards.py`.

## Conclusion first

The incident signature — mass deletion across the running worktree (committed
test files included) + stray tarball-only `wheels/` and `tuwunel-binaries/`
dirs at the repo root — is exactly what
`mercury_cli.update_release._swap_tree` does when `dst` is the live checkout.
That path has **no pytest-live-checkout guard** (unlike every sibling mutator),
and `_project_root()` resolves to the live checkout in-suite. Proven with two
read-only-safe probes + a RED regression test. No full-dir run was executed.

## Mechanism (exact lines, `hermes/mercury_cli/update_release.py`)

- L37–64 `_project_root()`: walks up for `bin/mercury` + `hermes/`. In any git
  checkout/worktree both exist at the repo root → returns the LIVE root.
  Import probe: `ur._project_root()` == worktree root. True.
- L167–191 `_swap_tree(src, dst)`: per tarball top-level entry,
  `shutil.rmtree(target)` then `copytree`/`copy2`. Tarball top level per
  `scripts/make-dist.sh` (L122–128, L152–186) is `bin/ hermes/ omp/
  wheels/ tuwunel-binaries/ …`. So a live-root swap rmtree's the real `bin/`,
  `hermes/` (**contains `hermes/tests/mercury_cli/*.py` — the deleted committed
  test files**), `omp/`, and plants tarball-only `wheels/` + `tuwunel-binaries/`
  at the root (**the stray dirs**). Signature match is 1:1.
- L437–439 `update_from_release()`: `root = _project_root(); _swap_tree(src, root)`
  with no guard. `update_release` exposes no `_pytest_owns_live_checkout`
  (probe: `hasattr` False; `main` True).
- L473–486 legacy junk sweep: `rmtree(root/"hermes"/{bin,omp,hermes,bridge})` —
  second deleter on the same live root.
- L90–94 `_record_build_id`: writes `root/.mercury-build-id` — live-root litter
  even on an otherwise-mocked run.
- L488–534 venv refresh + `_install_bundled_wheels(root/"wheels")`, L548–579
  `provision_if_missing()` / `refresh_for_update()` — all execute against the
  live root/venv/home once the swap proceeds.

Same fail-open shape in `hermes/mercury_cli/update_cmd.py`: `_update_via_zip`
(L1889) stages then copytrees over `_m().PROJECT_ROOT` (L2018, via
`_stage_replacement` L1204 / `_commit_staged_replacements` L1252) with no guard;
only the dirty-overlay check (L1745) stands in its way, and a clean worktree
passes it. Prior instance admitted in-tree:
`hermes/tests/mercury_cli/test_update_zip_symlink_reject.py` L89–97
("previously stomped on README.md … leaving 'ok\n' there") — since sandboxed
via `fake_root` (L104–109). The function itself is still unguarded.

Guard coverage elsewhere (the pattern to copy): `main.py` L9044–9057 +
L9106–9107, `_early_recovery.py` L416+, `update_cmd.py` L3134–3138 / L3168–3173
(markers only), `managed_scope.py` L41–69, `config.py` L3810, `auth.py`.

## Trigger surface in-suite (static audit, 720 files)

- AST scan: every function calling `_update_via_zip / update_from_release /
  _swap_tree / _commit_staged / cmd_update / provision_* / stash / markers`,
  cross-checked for `tmp_path`+`monkeypatch` sandboxing. Full output method in
  §Verification.
- Only in-suite driver of `update_from_release` sandboxes `_project_root`:
  `test_update_release_observatory.py` L89 (`lambda: self.root` in tmp). No
  CURRENT single file drives the release swap at the live root — the hole is
  fail-open production code, triggerable by any unsandboxed (present/future)
  driver or mock leak under a full-dir run. That is why single-file runs pass
  and full-dir runs stomp.
- Live-root contact with zero fixtures (brittle, must-fix hygiene):
  `test_cmd_update.py` L1022–1027
  (`test_update_rebuilds_desktop_that_disappears_mid_update` derives
  `desktop_dir` from the live imported `PROJECT_ROOT`; read-only today only
  because the build is mocked).
- cmd_update E2E tests run full post-update phases against live `PROJECT_ROOT`
  with only `subprocess.run` mocked (e.g. `test_update_yes_flag.py` L60–92 with
  `commit_count="1"`); each is one unmocked `shutil`/`Path` write away from the
  same class of stomp. No fail-closed root guard backs them.
- No `scope="session"` fixtures, no raw `os.chdir`, no `git clean/reset/checkout`
  against the live tree found in `mercury_cli/` tests. `os.environ[x]=` direct
  writes found (`test_argparse_flag_propagation.py` L120,
  `test_install_cua_driver.py` L832/877/911, `test_ignore_user_config_flags.py`
  L152–154) leak across tests if the test errors mid-flight — secondary suspect
  for order-dependence, not the deleter.

## Verification (minimal single-file runs only, `-p no:cacheprovider`)

1. `test_checkout_mutation_guards.py` alone (worktree cwd, shared venv):
   **8 passed** — marker/recovery guards hold; they do not cover the swap.
2. Read-only import probe (no writes): `ur` has no guard; `_project_root()`
   == live root; `_update_via_zip` refs `PROJECT_ROOT`, no guard. All True.
3. New `test_update_release_live_checkout_guard.py` (this branch): **2 failed**
   as designed — (a) guard-attribute assertion; (b) tripwire proves
   `_swap_tree` was called with
   `dst=<live worktree root>`. `git status` after: tree untouched (only the new
   untracked test file). Fails on current code, stomps nothing.

## Containment recommendation

1. **Quarantine (product, one small patch):** add `_pytest_owns_live_checkout`
   to `update_release.py` (predicate at install-root level:
   `root == Path(__file__).resolve().parents[2]` + `PYTEST_CURRENT_TEST`), and
   early-return non-zero in `update_from_release` + `_update_via_zip` before
   download/extract/swap — mirroring `main._recover_from_interrupted_install`
   (L9106). This turns the RED test GREEN and makes the whole suite fail-closed
   regardless of per-test mocks. Same for `_record_build_id` (skip under guard).
2. **Sandbox (tests):** give `test_cmd_update.py` L1022 a `tmp_path` root like
   its L1054 sibling; rule: no test touches imported-live `PROJECT_ROOT`
   without `monkeypatch.setattr(..., tmp_path)`.
3. **Split (process):** until (1) lands, NEVER run the full `mercury_cli/` dir
   from inside a worktree; run update/install subsets file-by-file with
   `-p no:cacheprovider`. After (1), full-dir runs are safe by construction.

## Deliverable

- `hermes/tests/mercury_cli/test_update_release_live_checkout_guard.py` — RED,
  hermetic (tripwires + tmp tarball; live root only read-resolved).
- This file. Commit hash on branch `agent/mercury-cli-stomper-probe`: see below.
