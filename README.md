# Mercury 🌡️

**A hybrid agent distribution: [Hermes](https://github.com/NousResearch/hermes-agent) by Nous Research + [omp](https://github.com/can1357/oh-my-pi) by can1357 — one install, one config, one agent that orchestrates and delegates.**

Mercury glues two proven halves into a single harness. The hermes half is
the conversation agent — memory, skills, scheduling, messaging platforms,
the CLI/TUI you chat in. The omp half is the coding engine — the same
batteries-included agent that drives LSP, DAP debuggers, real browsers,
and parallel subagents. In Mercury, hermes orchestrates and omp executes:
every coding task — down to a hello world — fans out to omp subagents,
and those subagents can spawn subagents of their own.

## Quick install (Linux / macOS / WSL2)

```bash
curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install.sh | bash -s -- https://github.com/fengwhang/mercury/releases/download/v0.0.1/mercury-0.0.1.tar.gz && source ~/.bashrc
```

(zsh: `&& source ~/.zshrc` · fish: `&& source ~/.config/fish/config.fish`)

The trailing `source` runs in YOUR shell (the pipe only covers the install),
so `mercury` works in the current terminal immediately — no new terminal needed.

One interactive session: preflight → uv + pinned venv → unpack the prebuilt
engines (no bun, no rust needed) → **`mercury setup` — the full wizard:
provider OAuth / Nous Portal login, model pickers, tools** (configures BOTH
engines) → optional Browser Use CLI, cua-driver, gateway → done. Then run
`mercury`.

**Layout** — everything lives under one home, hermes-style:

```
~/.mercury/                 the Mercury home (MERCURY_HOME)
├── mercury-agent/          the code tree (bin/, config/, hermes/, omp/, …)
├── bin/mercury             the command (managed bin dir; uv + browser-use too)
├── config.yaml             the ONE unified config (both engines)
├── SOUL.md MEMORY.md USER.md AGENTS.md   shared state, both engines
├── skills/                 the shared skills library
└── hermes/  omp/           engine-private state (sessions, auth, omp data)
```

| | |
|---|---|
| **One command** | `mercury` starts the chat; `mercury omp` drops you into the omp engine directly |
| **One config** | `~/.mercury/config.yaml` drives BOTH engines — four model slots, one approvals knob, per-engine subtrees |
| **Fail-hard models** | a fallback slot you didn't configure is an error, not a silent degradation |
| **Unified approvals** | `manual` / `smart` / `off` — one mode governs both engines; deny rules and the hardline floor survive every mode |
| **Shared state** | SOUL/MEMORY/USER and the skills library live at `~/.mercury/` and are read by both engines |
| **One key per capability** | `ZAI_API_KEY` serves zai search on both engines; the tool-provider union exposes all 24 omp search providers to the hermes side |
| **No model roles** | the role system (--smol/--slow/@task) is deleted, not stubbed — one model fans out everywhere |

Mercury runs anywhere hermes runs (Linux, macOS, WSL2; containerized
backends) with omp's native runtime **prebuilt**: the release binary
embeds the Rust natives (ripgrep, shell, text/grep/image ops) and runs
with only libc on the target — no bun, no rust toolchain, no node
(verified: the binary executes under a stripped PATH with a live
one-shot). Only optional embedding-model deps (fastembed,
onnxruntime-node) resolve on-demand, per upstream design.

**Updates are re-fetches, not compiles.** Each release rebuilds the
modded omp once on the build host and publishes a fresh tarball
(`scripts/make-dist.sh`); re-running the installer against the new
tarball replaces the tree in place while `~/.mercury` state survives.
End users never need a toolchain; contributors building from source see
[Development](#development).

**Feature-complete omp.** Mercury's patches touch omp's model
resolution, theme defaults, memory/skills paths, and the bridge — the
tool surface itself is unmodified: the DAP debugger (lldb/dlv/debugpy),
LSP integration, eval's browser + desktop control, native ripgrep/shell,
hashline edits, code review, subagents — all present, all upstream.

---

## Install

The quick-install command above is the whole story. Details:

- **The wizard is `mercury setup`** — the full interactive setup (provider
  OAuth / Nous Portal / API keys, model pickers, TTS, tools, gateway), run
  automatically at the end of the install. It configures BOTH engines: the
  omp side inherits your models, approval mode, and deny rules via the
  config bridge. Re-run any time with `mercury setup`.
- **Model slots are fail-hard**: default / fallback / delegate_model /
  delegate_fallback in `~/.mercury/config.yaml`; no fallback configured is
  an error, not a silent degradation. `mercury omp-sync` re-syncs the omp
  engine after hand edits.
- **Keys** live in `~/.mercury/hermes/.env` (chmod 600, never in the repo).
- **Approval mode** (`manual` / `smart` / `off`) is ONE knob for both
  engines; `off` = permanent yolo with deny-rules + the hardline floor
  still active.
- Flags: `--skip-setup`, `--non-interactive`, `--skip-browser`,
  `--skip-computer-use`, `--no-skills`, `--skip-gateway`, `--dir PATH`,
  `--ensure browser,computer-use`.
- Updates: `mercury update` pulls the latest release tarball from this
  repo (tarball installs) or `git pull` (dev checkouts).

Manual path: clone, `bash install.sh` with no URL (works against the
existing tree), or see `scripts/make-dist.sh` to build the tarball
yourself.

Then:

```bash
mercury          # the chat (hermes half, orangered skin, pink session label)
mercury omp      # the omp engine as its own program (its own TUI, themes at ~/.mercury/omp/themes/)
```

## The model story

Four slots at the top of the unified config — the entire model story for
both engines:

```yaml
models:
  default: <provider/model>           # hermes sessions + omp main loop
  fallback: <provider/model>          # REQUIRED — missing = hard fail, no silent fallback
  delegate_model: <provider/model>    # what omp subagents run under
  delegate_fallback: <provider/model> # REQUIRED for delegation — hard fail if missing
```

- Per-request failover on both engines: hermes fails over mid-turn and
  re-arms the primary next turn; omp walks its chain per request.
- Any provider either engine supports: zai, anthropic, openai, openrouter,
  google, xai, deepseek, 60+ more, plus local OpenAI-compatible servers.
- There are no model roles. No @smol, no --slow. One model per slot,
  explicit, everywhere.

## Approvals — one knob, both engines

```yaml
approvals:
  mode: "smart"   # manual | smart | off
```

- `manual` — prompt for write/exec (hermes) / always-ask (omp)
- `smart` — the default: read+workspace-write auto-approved, exec prompts
- `off` — permanent yolo: no prompts anywhere (this is what the installer's
  yolo option writes)

In every mode: your `approvals.deny` globs are translated to omp
`bash.patterns` deny rules at every spawn (denies fire before any
bypass), and the hardline floor (disk-wipe-at-root, block-device
overwrites, host shutdown) is unconditional. Yolo is an explicit choice,
never a default.

## What each half gives you

**hermes half (the orchestrator):** terminal TUI with slash commands ·
memory that persists across sessions (MEMORY/USER/SOUL) · self-improving
skills (agentskills.io format) · cron scheduling with delivery to any
platform · the messaging gateway (Telegram, Discord, Slack, WhatsApp,
Signal, ~20 platforms) · skins and theming · computer-use desktop control
· voice.

**omp half (the engine):** every omp capability under delegation —
persistent Python/JS cells with tool re-entry · LSP wired into every
write · real DAP debugger sessions (lldb, dlv, debugpy) · native
ripgrep/shell in-process · hashline edits · code review with ranked
verdicts · 23 ranked web-search providers · real browser + desktop
control · first-class subagents (isolated worktrees, typed results).

**The bridge between them:**
- `delegate_task` — hermes' subagent tool; children run as omp one-shots
  with your delegate slots, C2-translated deny rules, and full recursion.
- `/omp` — deterministic command: your text travels to omp as a single
  argv element, byte-for-byte, no LLM in between.
- `omp_direct` cron — schedule a job that fires omp directly, no hermes
  agent turn in the loop.
- Tool-provider union — hermes can search/scrape through ANY of omp's 24
  providers (`omp-bridge:<id>`), so one key lights up both engines.

## Configuration

ONE file: `~/.mercury/config.yaml`. Top level holds `models:` (four
slots) and `approvals:` (one knob). Per-engine settings live under
subtrees each engine owns:

```yaml
models:
  default: anthropic/claude-sonnet-4-6
  fallback: anthropic/claude-haiku-4
  delegate_model: anthropic/claude-sonnet-4-6
  delegate_fallback: anthropic/claude-haiku-4

approvals:
  mode: "smart"

hermes:            # hermes-private settings (explicit keys WIN over slots)
  display:
    skin: default
omp:               # bridge-rendered (approvalMode, deny patterns, retry chain)
  tools:
    approvalMode: "write"
```

State layout at `~/.mercury/`:

```
~/.mercury/
  config.yaml     # the ONE config
  SOUL.md         # persona (both engines read; hermes writes)
  MEMORY.md USER.md
  skills/         # shared skills library — both engines, two-way
  HERMES.md       # hermes-only system prompt supplement (edit freely)
  OMP.md          # omp-only system prompt supplement (edit freely)
  hermes/         # hermes-private state (sessions, plugins, .env keys)
  omp/            # omp-private state (themes/, settings)
```

Skins: full hermes skin support — built-ins plus user YAML at
`~/.mercury/hermes/skins/`, `display.skin` selects, `/skin` switches
live. The Mercury default skin is orangered with a pink session label;
every key is skin-overridable. omp themes: JSON at
`~/.mercury/omp/themes/<name>.json`, `theme.dark`/`theme.light` select.

## Development

The repo vendors both upstreams at pinned tags (`PINS.txt` records tag +
commit). Every fork change is marked `HERMES-OMP PATCH` in-source for
clean upstream re-pins. Engine directories carry engines only;
`config/` holds every hand-editable default (SOUL.md, MEMORY.md,
USER.md, AGENTS.md, HERMES.md, OMP.md, config.yaml schema) — seeded to
`~/.mercury/` on first boot and never overwritten after you edit them.

### Layout

```
bin/mercury      the launcher (env composer — sets MERCURY_HOME/CONFIG,
                 forces engine homes, self-locates its repo)
install.sh       one-command installer (tarball URL or in-place)
config/          shipped defaults for everything hand-editable
bridge/          four-slot model config renderer (fail-hard)
hermes/          the hermes engine (vendored, patched, renamed modules)
omp/             the omp engine (vendored, patched; dist/omp = release
                 binary with embedded Rust natives)
scripts/         make-dist.sh (release tarball), vm-sim.sh (fresh-python
                 verification), dev verify scripts
```

### Build & release

```bash
# one-time on the build host: bun + rust toolchain, then
cd omp/packages/coding-agent && bun run build     # compiles dist/omp
cd ../../.. && cd hermes/ui-tui && bun run build  # compiles dist/entry.js
cd ../../.. && scripts/make-dist.sh               # -> dist/mercury-0.0.1.tar.gz + sha256

# install from a local tree (dev loop)
bash install.sh

# python side
cd hermes && uv venv .venv --python '>=3.11,<3.14' && uv pip install -e .
PYTHONPATH=hermes hermes/.venv/bin/python ...     # run against the repo
```

Release workflow: commit → rebuild omp + ui-tui → `make-dist.sh` →
publish the tarball → users re-run `install.sh <tarball-url>` (tree
replaced in place; `~/.mercury` state survives). `scripts/vm-sim.sh`
verifies the fresh-interpreter path (uv venv, no shared state) before
shipping.

### Re-pinning upstream

Check out the new tag inside `hermes/` / `omp/`, re-apply the patch
series (grep `HERMES-OMP PATCH` — 17 omp files + the hermes-side
markers), rebuild both engines, re-run the bridge + delegation + banner
checks. omp ships frequent releases; re-pin deliberately, not eagerly.

## Credits & licenses

Mercury is a hybrid distribution of
[Hermes](https://github.com/NousResearch/hermes-agent) by
[Nous Research](https://nousresearch.com) (MIT) and
[omp / oh-my-pi](https://github.com/can1357/oh-my-pi) by
[can1357](https://github.com/can1357) (MIT; fork of
[Pi](https://github.com/badlogic/pi-mono) by Mario Zechner).

Each vendored half carries its upstream license and notices; see
`hermes/LICENSE`, `omp/LICENSE`, and `omp/THIRD-PARTY-NOTICES.txt`.
Upstream documentation remains the reference for each half:
[hermes docs](https://hermes-agent.nousresearch.com/docs) ·
[omp docs](https://omp.sh/docs).
