# v0.4.5-nightly human-testing scope

This is a scoped local build, not publication approval. Product version is 0.4.5.
Base: `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc`; previous release: `a55e9c431acf574cc105e3d69fa6565656a4b2fd`.
The owner scope pivot supersedes earlier exhaustive completion briefs. No feature workers or checkpoint jobs are restarted.

## READY FOR HUMAN TESTING

- Already integrated provider-exception fallback (`c1147f2b`); voice/parity changes inherited from the previous release, not re-applied or advertised as new complete parity.
- Reviewed Linux CUA (`c205980f` through `fdea31f4`): compatibility, local diagnostics, telemetry-off precedence and Hermes screenshot staging/delivery. Retained review evidence: 104 parent contracts and 113 native assertions. This is NOT the new headless Bot Screen or canonical OMP CUA parity.
- Standalone lifecycle (`ca7e7891634f46477b6f8abcab6ad98738e18d2f`): durable delegated task checkpoints, honest interruption/terminal outcomes, reconnect feed receipts, authenticated restart ownership/provenance, local maintenance admission, tool-result rendering. Retained evidence: 947 passed/4 skipped at executable-equivalent `1eeb1a9b`, 16 native checkpoints and real generic registered-process recovery. Native Hermes-parent hub acceptance remains EXTERNAL/unfinished and is NOT included.
- Narrow approval bridge shutdown (`8e50da966682ae826b49d7f4f7757afb94a096d1`): only its two-file patch is imported as a release-owned standalone slice. Independent narrow review PASS, 83 approval contracts /170 combined focus and 6.5–47.8 ms measured shutdown retained; strict cold fanout remains non-green, not a release claim.
- Minimal runtime privacy boundaries: no banner/vendor version poll, CUA automatic repair/update thread, lazy dependency installation, marketplace automatic update, or startup online provider inventory. Cold incidental OpenRouter metadata uses local inventory. Native CUA strips provider/cloud credentials after terminal passthrough. Explicit setup/manual provisioning is separate; no packages are fetched in this release pipeline.
- Type fixture `04406f55` is included only because the actual canonical typegate found its asynchronous credential-resolver mismatch. Native/internal package ABI versions remain 18.1.6.

## Explicit safe exclusions from inherited integration

The inherited combined-stats integration (`e90392bf`, CLI `2a6df01c`) is reverted: no new dual-engine SQLite snapshot/API is exposed. Stock OMP statistics is not advertised as the new combined overhaul.
OMP `share_file` returns an explicit unavailable/error result and never stages bytes while its authorization/secret-path repair lacks approval. Hermes reviewed screenshot delivery is separate. No unsafe sharing success is fabricated.
An ancestry-carrying approval merge was immediately reverted before compilation. No native hub implementation from that ancestry is present in packaged source. The two-file approval patch was reapplied independently, not the `344853` hub-verification merge.

## SHELVED-WIP and FAILED-REVIEW/EXCLUDED

All paths below are under `/home/user/Documents/mercury-worktrees/`; evidence handles under `/tmp/mercury-v045-codex-evidence/` are retained verbatim under `/home/user/Documents/mercury-releases/v0.4.5-nightly/retained-evidence/` where listed in the recovery manifest.

| Feature | State at pivot / why excluded | Reusable evidence / exact next checkpoint |
| --- | --- | --- |
| Native Hermes-parent hub parity/L1 and hub lifecycle | SHELVED-WIP; hub initial `6f857432` FAILED review; repair `e6b61a48` and lifecycle work intentionally cancelled, not a new product failure | `hub-review.json`, `hub-lifecycle.json`; independently prove real native coordinator+peer shutdown/resume and accepted messages/replies exactly once, then L1 exit/revocation/cascade. Generic lifecycle process proof is not native-hub proof. |
| mLounge '+' spawn (`v045-codex-mlounge-spawn`, `72e943b3`) | FAILED-REVIEW/EXCLUDED; 8 unique blockers | `mlounge-spawn-review.json`; repair exact independent findings and verify packaged profile, auth and native spawn contracts, not golden DOM alone. |
| mLounge exit cascade (`v045-codex-mlounge-exit`, `27271f61`) | FAILED-REVIEW/EXCLUDED; 5 findings | `mlounge-exit-review.json`; fix exact cascade/revocation/UI findings and independent end-to-end proof. Existing gateway `!exit` protection is retained. |
| mLounge media (`v045-codex-mlounge-media`, `a0127677`) | FAILED-REVIEW/EXCLUDED; 1 independent blocker | `mlounge-media-review.json`; resolve packaged/runtime independent blocker and rerun exact reproduction. |
| Gateway-specific no-X UI | SHELVED-WIP; no independently ready small committed source selected | `gateway-close-protection.json`, `v045-gateway-close-protection`; verify gateway and non-gateway close behavior without weakening existing protection. |
| Bot Screen backend / Xfce runtime and browser attach | SHELVED-WIP; intentionally cancelled, report partial; attach-daemon-death confirmation unresolved | `bot-screen-runtime.json`, `bot-screen-runtime-final-review.json`; readonly/cold import 41 contracts reusable, not full approval. Prove daemon-death confirmation and review final source. No backend flags enabled here. |
| Native Xfce/TigerVNC QA | TEST-INFRA evidence retained / SHELVED-WIP; capture completed but input fixtures unfinished | `bot-screen-qa.json`; preserve native captures and fix exact input/fixture contracts before treating as product acceptance. |
| OMP canonical CUA / full configured-tools live parity | SHELVED-WIP; intentionally interrupted, partial reports, broker verticals green only | `omp-cua-parity.json`, `configured-tool-parity.json`; freeze final source, prove configured enabled/disabled tools and auth/profile projection on real parent/child entrypoints. Legacy SDK fixtures are not full live parity. |
| Optional setup desktop dependencies / provisioning script | SHELVED-WIP; 135 focused tests do not establish full approval; named projection epoch pending | `setup-computer-deps.json`, `v045-setup-computer-deps`, `v045-bot-screen-provision-script`; resolve named projection generation, then cache-only setup end-to-end in both channels. Script retained, not shipped as runtime installer. |
| Semantic notifications local/remote | SHELVED early WIP; intentionally cancelled during mapping/isolation | `mlounge-notification-policy.json`, `v045-mlounge-notification-{policy,client,producers}`; finish canonical event semantics and verify local/remote policy in actual client before enabling. |
| Share-file safety repair | SHELVED-WIP; stop retried, termination not established by late callbacks | `omp-share-safety.json`, `v045-omp-share-safety`; original dirty files remain untouched, pre-terminal capture withdrawn/not imported. Confirm writer quiescence, commit repair, obtain independent auth/secret/symlink/race review. OMP publication is excluded in this artifact. |
| Large core/model/cron/resolver/MCP overhaul (`v045-codex-core`, `7a1d59c7`) | FAILED-REVIEW/EXCLUDED | `core-independent-review.json`: ENV-1 newline bridge child-env injection; OMP-1 checkout borrowing, OMP-2 stale pin masking; MODEL-1 reasoning off loss, MODEL-2 fallback inconsistency, MODEL-3 non-idempotent config writes, MODEL-4 authority bypass; MCP-1 disabled tool catalog leak, MCP-2 enum loss. Repair pinned reproductions then independent rerun. Narrow MCP rename `5133d494` and resolver `19d83c78` NOT blindly imported. |
| Combined statistics overhaul (`v045-codex-stats`, `9814078e`) | FAILED-REVIEW/EXCLUDED; 13 probes | `stats-independent-review.json`: hot-journal garbage, replaced-path reads, incomplete-schema zeros, fork/tool doublecounts, order-dependent unknown pricing, profile routing/cache poisoning. Repair snapshot authorization/integrity and accounting invariants before reintroduction. |
| Product test-isolation patch (`v045-codex-test-isolation`, `dd155901`) | FAILED-REVIEW/EXCLUDED; 10 findings | `test-isolation-review.json`; no merge/no use as safety proof. External `safe-test-isolation.json`, `candidate-environment-reuse.json` and owned bwrap launcher are retained TEST-INFRA. |
| Strict cold spawn latency/performance work | SHELVED/non-green; narrow shutdown fix does not fix full cold fanout | `approval-latency.json`; keep existing timings, no new performance loop in this freeze. |
| Provider-only egress inventory beyond included ordinary startup seams | TEST-INFRA inventory, not global compliance proof | `provider-only-egress-review.json` pins fdea31f4. Current artifact has tested minimal startup boundaries; optional inherited download/explicit provider/setup paths are NOT claimed exhaustively audited or approved. No runtime acquisition is exercised or authorized by these builds. |
| Global baseline/comparator work | TEST-INFRA retained, publication gate not inferred | `baseline-coverage-review.json`: 71,288 primary IDs; supplement 178 missing nodes =117 pass/61 fail; 24 uncollectible declarations and 38 Bun occurrences across18 ambiguous display bases remain. Original datasets immutable. Unknown IDs are not grandfathered. |

## Recovery and verification contract

Complete branch/tree/HEAD/dirty status, patch hashes and untracked source archive hashes for all inventoried v045 worktrees are in `recovery-manifest.json` in the persistent release directory. Dirty snapshots are never import candidates. Seven stopped speculative writers were confirmed terminal by the parent; share-safety is explicitly unconfirmed. Four future checkpoints are disabled/paused per owner pivot receipt. No new timer or worker was created.

Pre-existing test/browser/hub processes are recorded by exact handles in `processes-at-pivot.txt`. This pipeline sends no signals to them, the owner gateway or display. Tests/builds use private bwrap PID/mount/network/user/IPC/UTS/cgroup namespaces, dropped capabilities, clear environment, private homes/run/proc and DEVNULL stdin/closed descriptors. No live install/config/systemctl/gateway restart, paid model request, GitHub API/token read, source download, npm audit or browser/ONNX acquisition occurs.

Build ordering: canonical version bump → clean committed source SHA → four sequential Linux target compilations → canonical archive packer (`git archive HEAD`) → four new nightly filename-correct checksum sidecars → extracted current-architecture Hermes and OMP smoke. Exact toolchain/cache manifests, commands, exits, logs and artifact hashes live in `pipeline/` and `artifact-manifest.json` beside the artifacts.

Local artifacts do not authorize publication. Publication requires focused contracts, actual typegate, fresh full-suite at the exact final SHA and a stable-ID baseline comparison with no unexplained new failures or unknown coverage. Gate status is recorded honestly in `nightly-freeze-build.json`; pending or failed is never described as passed. Later publishing handoff is independent of credentials and requires owner authorization after the gate is resolved.

## Exact pivot worktree inventory

| Worktree | Branch | HEAD | Dirty at pivot | Preservation |
| --- | --- | --- | --- | --- |
| `v045` | `agent/v045-no-grievances` | `4eb64a4509724ef8b32d3794686ae308cac8f0b1` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045` |
| `v045-auto-restart-admission` | `fix/v045-auto-restart-admission` | `2bbdfd4919e5da06a54b3474bd7417e293e39fb1` | no | `committed source; retained original tree` |
| `v045-baseline` | `agent/v045-baseline` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045-baseline-comparator-review` | `review/v045-baseline-comparator-review` | `8c373c7cd10e9ec06e111de6324d9ab2faf863bf` | no | `committed source; retained original tree` |
| `v045-baseline-coverage-review` | `review/v045-baseline-coverage-review` | `f9bb336ecd3efd7b0ff7261662a05d2508f88fdb` | no | `committed source; retained original tree` |
| `v045-baseline-run` | `agent/v045-baseline-run` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045-bot-screen-browser-binding` | `fix/v045-bot-screen-browser-binding` | `1ecb77478c46ddead570fb9a8cccd51d59f0faa4` | no | `committed source; retained original tree` |
| `v045-bot-screen-browser-daemon` | `fix/v045-bot-screen-browser-daemon` | `bf4178d7af07a4fcf352338b09142859a2f065d2` | no | `committed source; retained original tree` |
| `v045-bot-screen-cli` | `feature/v045-bot-screen-cli` | `86e1b45c4450caed0410d963e47bde29b7f4c5de` | no | `committed source; retained original tree` |
| `v045-bot-screen-cua-admission` | `fix/v045-bot-screen-cua-admission` | `eaf7a117fe7b07a2400dcf9c350ebca5c5fe0b7d` | no | `committed source; retained original tree` |
| `v045-bot-screen-cua-offline-cli` | `fix/v045-bot-screen-cua-offline-cli` | `d74b531d18d18b760cf5dd89b5b057a71308a071` | no | `committed source; retained original tree` |
| `v045-bot-screen-deps-command` | `docs/v045-bot-screen-deps-command` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-bot-screen-design-review` | `review/v045-bot-screen-design-review` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-bot-screen-env` | `feature/v045-bot-screen-env` | `b2e184591f770e0ccdb380bee18974fbc8425c2e` | no | `committed source; retained original tree` |
| `v045-bot-screen-env-daemon-rereview` | `review/v045-bot-screen-env-daemon` | `1e89ef80396b9a3a1442d239308554abb40b3dfb` | no | `committed source; retained original tree` |
| `v045-bot-screen-env-final-review` | `review/v045-bot-screen-env-final` | `cf648d6be371223939ed1c3ed119f1c62924ef2c` | no | `committed source; retained original tree` |
| `v045-bot-screen-env-review` | `review/v045-bot-screen-env` | `9573e4ef17e93e1f4af731a11fefba50bece98ff` | no | `committed source; retained original tree` |
| `v045-bot-screen-provision-script` | `tools/v045-bot-screen-provision-script` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-bot-screen-provision-script` |
| `v045-bot-screen-provision-script-review` | `review/v045-bot-screen-provision-script-review` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa` | `test/v045-bot-screen-qa` | `20ea6d49c6b606de82884fe1a8f242f9e4277e3a` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-bot-screen-qa` |
| `v045-bot-screen-qa-cwd-fix` | `fix/v045-bot-screen-qa-cwd` | `c92b296ba1da970438f43c0e5298147aedb7429d` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-entry` | `test/v045-bot-screen-qa-entry` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-event-fix` | `fix/v045-bot-screen-qa-native-events` | `e93c1ad0cc409e3ab190394522cd2c554d48c837` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-bot-screen-qa-event-fix` |
| `v045-bot-screen-qa-fixture` | `test/v045-bot-screen-qa-fixture` | `c77fc6ec6fdbbfc7a60091e14442eb44a77b20ba` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-fixture-fix` | `fix/v045-bot-screen-qa-fixture-isolation` | `866a95aa77d9a623d97200875adb76d74020e017` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-git-fix` | `fix/v045-bot-screen-qa-immutable-git` | `02fc0f6e0ad39f3645c42760085d7f0c79589760` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-hermes-native` | `test/v045-bot-screen-qa-hermes-native` | `1b938f824ed2cddb7c619f4a346567156c13bdde` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-omp-native` | `test/v045-bot-screen-qa-omp-native` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-bot-screen-qa-omp-native` |
| `v045-bot-screen-qa-recorder-fix` | `fix/v045-bot-screen-qa-recorder` | `fb25e2a8df720d226e1e67a63ac7fa7daa165876` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-runtime-native` | `test/v045-bot-screen-qa-runtime-native` | `acfe8b0cc13f4317c69440ac210f2939e573e9ae` | no | `committed source; retained original tree` |
| `v045-bot-screen-qa-trace-fix` | `fix/v045-bot-screen-qa-trace` | `3129adc5c01ef96f7b74c5800734543e5e541cb4` | no | `committed source; retained original tree` |
| `v045-bot-screen-runtime` | `feature/v045-bot-screen-runtime` | `1e89ef80396b9a3a1442d239308554abb40b3dfb` | no | `committed source; retained original tree` |
| `v045-bot-screen-runtime-final-review` | `review/v045-bot-screen-runtime-final` | `cf648d6be371223939ed1c3ed119f1c62924ef2c` | no | `committed source; retained original tree` |
| `v045-bot-screen-runtime-purity-review` | `review/v045-bot-screen-runtime-purity-fix` | `f6af3607214ae740987f6ea13b32e6aa3abf1f89` | no | `committed source; retained original tree` |
| `v045-bot-screen-runtime-rereview` | `review/v045-bot-screen-runtime-fixes` | `821d88f69508555c7b408b041c970feccbf7ea74` | no | `committed source; retained original tree` |
| `v045-bot-screen-runtime-review` | `review/v045-bot-screen-runtime` | `151e90f396aa5024a44bd5a30d1631190ecde05e` | no | `committed source; retained original tree` |
| `v045-broker-telemetry` | `agent/v045-broker-telemetry` | `6c04f86d8f3ba15a87e7917e2be52a02804392e8` | no | `committed source; retained original tree` |
| `v045-codex-approval-latency` | `fix/v045-approval-latency` | `8e50da966682ae826b49d7f4f7757afb94a096d1` | no | `committed source; retained original tree` |
| `v045-codex-baseline` | `review/v045-codex-baseline` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045-codex-core` | `fix/v045-codex-core` | `7a1d59c784098d0add9d7649da7a85dadcf38093` | no | `committed source; retained original tree` |
| `v045-codex-hub` | `feature/v045-hermes-hub-parity` | `6f85743248cdc7a01fd17103aaa1231e4ecbb73d` | no | `committed source; retained original tree` |
| `v045-codex-hub-lifecycle` | `fix/v045-hub-lifecycle` | `ebbc15cd9941528a44604393f03f4e87922b017f` | no | `committed source; retained original tree` |
| `v045-codex-hub-repair` | `fix/v045-codex-hub-repair` | `e6b61a48065163e9d77d004a9b084804c54dd0e3` | no | `committed source; retained original tree` |
| `v045-codex-hub-review` | `review/v045-codex-hub` | `6f85743248cdc7a01fd17103aaa1231e4ecbb73d` | no | `committed source; retained original tree` |
| `v045-codex-lifecycle` | `fix/v045-codex-lifecycle` | `ca7e7891634f46477b6f8abcab6ad98738e18d2f` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua` | `fix/v045-codex-linux-cua` | `c205980fd3081e75843aca60e53e288328839b51` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-cap-audit` | `review/v045-codex-linux-cua-cap-audit` | `d5c20f0862ef2b641e816fd143cb85cc9526ae84` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-delivery` | `fix/v045-codex-linux-cua-delivery` | `9cf626b32404ffdac60e18ac1c7237b95f134a50` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-delivery-review` | `review/v045-codex-linux-cua-delivery-child` | `cbe4613acd88bf8c7ca282724debac5f2812818e` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-delivery-review-green` | `review/v045-codex-linux-cua-delivery-child-green` | `9cf626b32404ffdac60e18ac1c7237b95f134a50` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-env` | `review/v045-codex-linux-cua-env` | `de312dad74dc198688fefba8434eab90a9b49bb3` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-harness` | `fix/v045-codex-linux-cua-harness` | `de312dad74dc198688fefba8434eab90a9b49bb3` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-independent-review` | `review/v045-codex-linux-cua-independent` | `c205980fd3081e75843aca60e53e288328839b51` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-policy` | `fix/v045-codex-linux-cua-policy` | `c2141a0fea32554ca14b12e6245777d22cdc56d8` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-policy-review` | `review/v045-codex-linux-cua-child-env` | `c2141a0fea32554ca14b12e6245777d22cdc56d8` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-qa` | `test/v045-codex-linux-cua-qa` | `d1f16f97efe1b089822848902fe8862f01f031aa` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-token-review` | `review/v045-codex-linux-cua-token` | `2bfb21792350101a76f293da38d4f43d6fdfbceb` | no | `committed source; retained original tree` |
| `v045-codex-linux-cua-tokens` | `fix/v045-codex-linux-cua-tokens` | `2bfb21792350101a76f293da38d4f43d6fdfbceb` | no | `committed source; retained original tree` |
| `v045-codex-mlounge-exit` | `feature/v045-mlounge-exit` | `27271f61f95333650f8e38f6f4431bab84e7e490` | no | `committed source; retained original tree` |
| `v045-codex-mlounge-media` | `feature/v045-mlounge-media` | `a01276770e6f40ad6b892343fb7e0b9589fbb199` | no | `committed source; retained original tree` |
| `v045-codex-mlounge-spawn` | `feature/v045-mlounge-spawn` | `72e943b30c2acf009b5edd3fdf0024491e4bc1d9` | no | `committed source; retained original tree` |
| `v045-codex-stats` | `fix/v045-codex-stats` | `9814078e4fd8647dd260885c6768e9e2062245c0` | no | `committed source; retained original tree` |
| `v045-codex-test-isolation` | `fix/v045-codex-test-isolation` | `dd155901eb6f0dbed48b3137420262e17f66e7b1` | no | `committed source; retained original tree` |
| `v045-codex-type-gate` | `fix/v045-codex-type-gate` | `04406f558af9407a66b2015ac0e8208fc07de069` | no | `committed source; retained original tree` |
| `v045-configured-tool-acceptance` | `test/v045-configured-tool-acceptance` | `6e28667029e1cf4def7cb3b69e4f7571b6fdb52b` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-configured-tool-acceptance` |
| `v045-configured-tool-broker-fix` | `fix/v045-configured-tool-broker-review` | `b892e8a9178b47273f95b6bc230d972c710a1017` | no | `committed source; retained original tree` |
| `v045-configured-tool-effects` | `feature/v045-configured-tool-effects` | `bf7e90e75d072f1cfefad5632495a3ea80216f59` | no | `committed source; retained original tree` |
| `v045-configured-tool-handoff` | `feature/v045-configured-tool-handoff` | `1edcc4d78ebad7ec575a84040fa959dfb0fdbaa0` | no | `committed source; retained original tree` |
| `v045-configured-tool-inventory` | `test/v045-configured-tool-inventory` | `f9561f7839bf843d3e5b8640f37b44dd8f0e15c8` | no | `committed source; retained original tree` |
| `v045-configured-tool-mcp-family` | `feature/v045-configured-tool-mcp-family` | `70c6da93d62946c2a8fc86fc6c0f224473bd6c6e` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-configured-tool-mcp-family` |
| `v045-configured-tool-memory-family` | `feature/v045-configured-tool-memory-family` | `58031c7139b7c59e4ea7bd0f29ad9d445293dd8a` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-configured-tool-memory-family` |
| `v045-configured-tool-omp` | `feature/v045-configured-tool-omp` | `acd993c652119f59ff69b217f4c5052fad3ebafa` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-configured-tool-omp` |
| `v045-configured-tool-parity` | `feature/v045-configured-tool-parity` | `57641950449fa9432f172d9f7b4dc88270efc038` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-configured-tool-parity` |
| `v045-configured-tool-session` | `feature/v045-configured-tool-session` | `a18d73b215df3c7e6dfbac2a9e46fcbcda714792` | no | `committed source; retained original tree` |
| `v045-configured-tool-transport` | `feature/v045-configured-tool-transport` | `a09845d09e5030ba3cfa7ecaefec86beda4fbc0e` | no | `committed source; retained original tree` |
| `v045-core-independent-harness` | `review/v045-core-independent-harness` | `96a9c8f015e7d585b6892b51b6b2f8b47d5be07c` | no | `committed source; retained original tree` |
| `v045-core-independent-review` | `review/v045-core-independent-review` | `7a1d59c784098d0add9d7649da7a85dadcf38093` | no | `committed source; retained original tree` |
| `v045-cua-parent-check` | `review/v045-cua-parent-check` | `c205980fd3081e75843aca60e53e288328839b51` | no | `committed source; retained original tree` |
| `v045-debug-reporting` | `agent/v045-debug-reporting` | `e1c41f75d4f9328f1295543783cf6a354582b874` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-debug-reporting` |
| `v045-env-final-cua-admission` | `review/v045-env-final-cua-admission` | `cf648d6be371223939ed1c3ed119f1c62924ef2c` | no | `committed source; retained original tree` |
| `v045-env-final-cua-native` | `review/v045-env-final-cua-native` | `cf648d6be371223939ed1c3ed119f1c62924ef2c` | no | `committed source; retained original tree` |
| `v045-fallback` | `agent/v045-fallback` | `c1147f2b8bbc8e9a1302582b2373daeccd124aad` | no | `committed source; retained original tree` |
| `v045-fallback-fix` | `fix/provider-exception-fallback` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045-feed-reconnect-receipts` | `fix/v045-feed-reconnect-receipts` | `3f40da4b3601032b97b61c27e04b2e3e3a87a097` | no | `committed source; retained original tree` |
| `v045-gateway-close-protection` | `fix/v045-gateway-close-protection` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-hermes-tool-provenance` | `feature/v045-hermes-tool-provenance` | `3df684f2c4c91da59a3e40d076fb811c89d8bd4e` | no | `committed source; retained original tree` |
| `v045-hub-compiled-goal` | `review/v045-hub-compiled-goal` | `872c94e4df900a6075ff06bed1752effb2be4a42` | no | `committed source; retained original tree` |
| `v045-hub-native-lifecycle` | `fix/v045-hub-native-lifecycle` | `dedf6b60fa5cea69561311675a5e3c15dd035061` | no | `committed source; retained original tree` |
| `v045-hub-owner-policy` | `fix/v045-hub-owner-policy` | `8f06efd0a4116378468fbdfd1b263322f569c8ae` | no | `committed source; retained original tree` |
| `v045-integration` | `agent/v045-integration` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-lifecycle` | `agent/v045-lifecycle` | `223166be99d224645fd6425db8f9ae0dbc88adca` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-lifecycle` |
| `v045-lifecycle-boot-order` | `agent/v045-lifecycle-boot-order` | `223166be99d224645fd6425db8f9ae0dbc88adca` | no | `committed source; retained original tree` |
| `v045-lifecycle-child-route` | `agent/v045-lifecycle-child-route` | `3bcffa304ff6089091e21aa8b2cf4a2c9d9e5f4e` | no | `committed source; retained original tree` |
| `v045-lifecycle-expired-route` | `agent/v045-lifecycle-expired-route` | `ac935d12b6d0d627d49b961bccabd2ce61ca9c87` | no | `committed source; retained original tree` |
| `v045-lifecycle-part-cache` | `agent/v045-lifecycle-part-cache` | `26a0035b18466b9dfbe4af8a51daa717dfe9e47b` | no | `committed source; retained original tree` |
| `v045-mcp-rename` | `fix/mercury-tools-mcp-server-module` | `5133d494ea5cb312f152f7e544d979cc6445187d` | no | `committed source; retained original tree` |
| `v045-media-build-review` | `review/v045-media-build-review` | `a01276770e6f40ad6b892343fb7e0b9589fbb199` | no | `committed source; retained original tree` |
| `v045-media-server-review` | `review/v045-media-server-review` | `a01276770e6f40ad6b892343fb7e0b9589fbb199` | no | `committed source; retained original tree` |
| `v045-mlounge-notification-client` | `feature/v045-mlounge-notification-client` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-mlounge-notification-client` |
| `v045-mlounge-notification-policy` | `feature/v045-mlounge-notification-policy` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-mlounge-notification-producers` | `feature/v045-mlounge-notification-producers` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-omp-bin` | `fix/omp-binary-resolution` | `19d83c78455ee4fdf1924750a7f645049243e716` | no | `committed source; retained original tree` |
| `v045-omp-cua-admission` | `feature/v045-omp-cua-admission` | `1fe6acb942effb33010d3c615533d2678b615a38` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-omp-cua-admission` |
| `v045-omp-cua-browser-scope` | `feature/v045-omp-cua-browser-scope` | `3adefc4519e5407f16aa373b6d79034902e57cf9` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-omp-cua-browser-scope` |
| `v045-omp-cua-legacy-live` | `fix/v045-omp-cua-legacy-live` | `ebef3f0340a52be20827903ed765b613817d1a8a` | no | `committed source; retained original tree` |
| `v045-omp-cua-live-review` | `review/v045-omp-cua-live-review` | `fd431e54644109953ed9b84b3b18d007ef1e3454` | no | `committed source; retained original tree` |
| `v045-omp-cua-mcp` | `feature/v045-omp-cua-mcp` | `b14ed730bde3c5790904dcc49aafff4a93d7c364` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-omp-cua-mcp` |
| `v045-omp-cua-parity` | `feature/v045-omp-cua-parity` | `8b98c9f46f31398f22fd9d7b298d69d74dbe0d0a` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-omp-cua-parity` |
| `v045-omp-native-composite-fixture` | `test/v045-omp-native-composite-fixture` | `a3adf82d77eca3e64c948b893c219809d5b0c4f4` | no | `committed source; retained original tree` |
| `v045-omp-share-authority-map` | `review/v045-omp-share-authority-map` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-omp-share-safety` | `fix/v045-omp-share-safety` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-omp-share-safety` (withdrawn, unconfirmed writer) |
| `v045-otlp-telemetry` | `agent/v045-otlp-telemetry` | `c5f0ab70d076789e020b0bc90760d1abc198f690` | no | `committed source; retained original tree` |
| `v045-outbound` | `agent/v045-outbound` | `5a5d5e0ce5352ca08efb6bddf92f7b1dacd3c9c3` | no | `committed source; retained original tree` |
| `v045-outbound-residues` | `agent/v045-outbound-residues` | `d503f842635e814a5912e9e98dd80e2d2663e7b3` | no | `committed source; retained original tree` |
| `v045-plugin-telemetry` | `agent/v045-plugin-telemetry` | `a27dc4dae3fb1c26885c94def997419949d3709b` | no | `committed source; retained original tree` |
| `v045-provider-egress-hermes` | `review/v045-provider-egress-hermes` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-provider-egress-omp` | `review/v045-provider-egress-omp` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-provider-egress-web` | `review/v045-provider-egress-web` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-provider-only-egress-review` | `review/v045-provider-only-egress` | `fdea31f4f8edccf220a495e6a0ce9411ff64e0cc` | no | `committed source; retained original tree` |
| `v045-python-exporters` | `agent/v045-python-exporters` | `0638557161d0d93640e5b862eeb9d32cad7c8b4d` | no | `committed source; retained original tree` |
| `v045-reconnect-identity` | `fix/v045-reconnect-identity` | `cd6b3ad8bba94e5d749c534f716096420cd870b7` | no | `committed source; retained original tree` |
| `v045-removal` | `agent/v045-removal` | `a2c5f8c15df2a574c8e927d4c9e3eb1e7d89afc8` | no | `committed source; retained original tree` |
| `v045-restart-drain` | `fix/restart-drain-subagent-state` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045-restart-fix` | `fix/stuck-loop-planned-restart` | `c63940db29a8f5bbc518467f54be72b1f4bc80df` | no | `committed source; retained original tree` |
| `v045-restart-owner-registry` | `fix/v045-restart-owner-registry` | `50da797a63124e8a4be561bf8df38f6f7cd914c9` | no | `committed source; retained original tree` |
| `v045-restart-provenance` | `fix/v045-restart-provenance` | `a7e8b225108257a73c457e2b45cc165209bce4ca` | no | `committed source; retained original tree` |
| `v045-room-reconcile` | `agent/v045-room-reconcile` | `6c7ea1a3f1c14faacefcd8e107ec9e1240a42047` | no | `committed source; retained original tree` |
| `v045-setup-computer-deps` | `feature/v045-setup-computer-deps` | `f4c8619988526021d866962d084f485d37e20de2` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-setup-computer-deps` |
| `v045-setup-deps-installer` | `feature/v045-setup-deps-installer` | `13f013b3545817493e6f80f61e1e32ee6a5408e2` | no | `committed source; retained original tree` |
| `v045-stats` | `feature/mercury-stats` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045-stats-independent-review` | `review/v045-stats-independent-review` | `6d14a84c9606bea0fb38f86cf2a3f47f638e9980` | no | `committed source; retained original tree` |
| `v045-stats-review-accounting` | `review/v045-stats-review-accounting` | `e164556d76ac0726d01b2dfa40f09e5f05f44254` | no | `committed source; retained original tree` |
| `v045-stats-review-security` | `review/v045-stats-review-security` | `8a5ccb75081d9d8f59ba1e365d6e1b5e767180cb` | no | `committed source; retained original tree` |
| `v045-stats2` | `feature/mercury-stats-both` | `5ef13e6c338781e0d29cf54ace49c33e2c5d85f4` | no | `committed source; retained original tree` |
| `v045-stats2-base` | `` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045-subagent-durability` | `fix/subagent-durability-and-tool-render` | `6c7ea1a3f1c14faacefcd8e107ec9e1240a42047` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-subagent-durability` |
| `v045-tests-hermes` | `agent/v045-tests-hermes` | `e4efe5ecec25517000830ce0eb26ae95bc461c3f` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-tests-hermes` |
| `v045-tests-omp` | `agent/v045-tests-omp` | `e4efe5ecec25517000830ce0eb26ae95bc461c3f` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-tests-omp` |
| `v045-tool-render` | `agent/v045-tool-render` | `6c7ea1a3f1c14faacefcd8e107ec9e1240a42047` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-tool-render` |
| `v045-tool-screen-cancel` | `feature/v045-tool-screen-cancel` | `9d2e3f849f45a067326180f8bd05a8b88cbf7428` | no | `committed source; retained original tree` |
| `v045-training-assets` | `agent/v045-training-assets` | `21bd463d41e379f844069b5396a34cb8e7b957ef` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045-training-assets` |
| `v045-verify-fixture-contract` | `review/v045-verify-fixture-contract` | `dd155901eb6f0dbed48b3137420262e17f66e7b1` | no | `committed source; retained original tree` |
| `v045-verify-isolation-evidence` | `review/v045-verify-isolation-evidence` | `dd155901eb6f0dbed48b3137420262e17f66e7b1` | no | `committed source; retained original tree` |
| `v045-verify-mlounge-exit` | `review/v045-verify-mlounge-exit` | `27271f61f95333650f8e38f6f4431bab84e7e490` | no | `committed source; retained original tree` |
| `v045-verify-mlounge-exit-backend` | `review/v045-verify-mlounge-exit-backend` | `27271f61f95333650f8e38f6f4431bab84e7e490` | no | `committed source; retained original tree` |
| `v045-verify-mlounge-exit-backend-base` | `review/v045-verify-mlounge-exit-backend-base` | `de312dad74dc198688fefba8434eab90a9b49bb3` | no | `committed source; retained original tree` |
| `v045-verify-mlounge-exit-client` | `review/v045-verify-mlounge-exit-client` | `27271f61f95333650f8e38f6f4431bab84e7e490` | no | `committed source; retained original tree` |
| `v045-verify-mlounge-media` | `review/v045-verify-mlounge-media` | `a01276770e6f40ad6b892343fb7e0b9589fbb199` | no | `committed source; retained original tree` |
| `v045-verify-mlounge-spawn` | `review/v045-verify-mlounge-spawn` | `72e943b30c2acf009b5edd3fdf0024491e4bc1d9` | no | `committed source; retained original tree` |
| `v045-verify-spawn-native` | `review/v045-verify-spawn-native` | `72e943b30c2acf009b5edd3fdf0024491e4bc1d9` | no | `committed source; retained original tree` |
| `v045-verify-spawn-profile` | `review/v045-verify-spawn-profile` | `72e943b30c2acf009b5edd3fdf0024491e4bc1d9` | no | `committed source; retained original tree` |
| `v045-verify-spawn-types` | `review/v045-verify-spawn-types` | `72e943b30c2acf009b5edd3fdf0024491e4bc1d9` | no | `committed source; retained original tree` |
| `v045-verify-spawn-types-base-3834126` | `review/v045-verify-spawn-types-base-3834126` | `de312dad74dc198688fefba8434eab90a9b49bb3` | no | `committed source; retained original tree` |
| `v045-verify-test-isolation` | `review/v045-verify-test-isolation` | `dd155901eb6f0dbed48b3137420262e17f66e7b1` | no | `committed source; retained original tree` |
| `v045-voice` | `agent/v045-voice` | `806c88201b798cde7f6e537b4cda6855ba32e40e` | no | `committed source; retained original tree` |
| `v045fix-hermes` | `agent/v045fix-hermes` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045fix-nous` | `agent/v045fix-nous` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | no | `committed source; retained original tree` |
| `v045fix-omp` | `agent/v045fix-omp` | `a55e9c431acf574cc105e3d69fa6565656a4b2fd` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/v045fix-omp` |
| `base-source` | `` | `91a2bb7b0051702e1700f773fe8057dc83f962ec` | yes | `/home/user/Documents/mercury-releases/v0.4.5-nightly/recovery/base-source` |
