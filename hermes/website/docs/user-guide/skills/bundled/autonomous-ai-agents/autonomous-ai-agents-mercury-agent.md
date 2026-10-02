---
title: "Mercury Agent — Configure and operate Mercury, its engines and Observatory"
sidebar_label: "Mercury Agent"
description: "Configure and operate Mercury, its engines and Observatory"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Mercury Agent

Configure and operate Mercury, its engines and Observatory.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/autonomous-ai-agents/mercury-agent` |
| Version | `4.0.0` |
| Author | Hermes Agent + Teknium |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `hermes`, `setup`, `configuration`, `multi-agent`, `spawning`, `cli`, `gateway`, `bots`, `bot-mode`, `features`, `themes`, `skins`, `desktop-plugins`, `tui-widgets`, `petdex`, `development` |
| Related skills | [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`opencode`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-opencode) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Mercury loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Mercury

Mercury combines its Hermes conversation engine and its omp coding engine with
shared model settings, credentials, skills, and an Observatory. mLounge is the
browser frontend fork of The Lounge; MIRC is Mercury's chat transport fork.

## Verify the fork first

Use `mercury --help`, the relevant subcommand's `--help`, `/help`, and the
Mercury source/README at https://github.com/fengwhang/mercury. For nightly,
use `mercury-nightly` consistently, including configuration and profile commands.
Upstream Hermes and omp documentation explains inherited features, but Mercury's
source takes precedence for profiles, delegation, approvals, models, and services.
Do not install stock Hermes or omp to repair a Mercury installation.

## Everyday commands

```bash
mercury setup
mercury model
mercury doctor
mercury chat -q "Research this issue"
mercury omp
mercury setup observatory
mercury observatory login
mercury observatory doctor
mercury observatory restart
```

The login card gives the web URL and MIRC connection settings. A Tailscale MIRC
host uses its MagicDNS name when available; enter that host without `http://`.
`!restart` in the managed gateway room restarts the full Observatory, including
when mLounge is not installed. Observatory agent sessions persist until `!exit`;
transport reconnects and closing a browser are not session termination commands.

## Paths and profiles

`$MERCURY_HOME` is the installation home: normally `~/.mercury` for stable or
`~/.mercury-nightly` for nightly. `$HERMES_HOME` is the selected Hermes runtime
home. The launchers set these variables; do not overwrite them in a shell tool.

- `mercury config path` identifies the selected YAML configuration.
- `mercury config env-path` identifies the shared secrets file.
- `$MERCURY_HOME/skills/` is the shared skill library read by both engines.
- Default Markdown instructions live in `$MERCURY_HOME/config/`.
- Named profiles live in `$MERCURY_HOME/hermes/profiles/<name>/`, with their own
  `config.yaml`, runtime state, and `config/` instruction folder.

Read [references/profiles.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/profiles.md) for creation, cloning,
prompt files, and launching either engine. Read
[references/project-context-files.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/project-context-files.md) when
choosing between profile instructions and repository rules.

## Models, reasoning, and permissions

Setup selects default, fallback, delegate, and delegate fallback, with reasoning
and context-window selection immediately after each model selection. Extra retry models require editing
`models.fallback_chain` or `models.delegate_fallback_chain` in the selected
config. Each of the four models can use a different provider. Both engines honor provider-advertised effort choices and mandatory
reasoning; do not invent effort tiers when the API exposes no selector.
Unknown/offline effort metadata retains compatibility behavior. Context choices
use the serving provider's advertised default/maximum: a single advertised
window gets Default/Custom, and missing metadata gets automatic detection or
Custom. `models.context_windows` shares per-model budgets across both engines.
Compaction is enabled by default. `mercury setup context` selects 50%, 75%, or
a custom percentage shared with OMP. The Context Engine Plugin Tools checkbox
controls plugin tools, not the built-in compressor.

omp has exactly one model role, `task`. Task descriptions and profile personas
are not additional model roles. Do not add planner, reviewer, leaf, or
orchestrator model roles to omp configuration.

Hermes and omp approval options are distinct. Use `mercury setup approvals`, or
`mercury setup hermes-approvals` / `mercury setup omp-approvals`. Read
[references/security-privacy.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/security-privacy.md) before changing
permissions. A child's approval travels through its ancestors to the user,
including across engine boundaries; delegation does not bypass permissions.

## Delegation and long-running work

Hermes `delegate_task` runs Mercury omp children. Use the live tool schema for
single, batch, background, and follow-up operations. Children inherit the selected
profile's model settings and instruction files. Read
[references/background-systems.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/background-systems.md) for the
current delegation contract, cron, and skill curation.

For a separate interactive profile, run `mercury -p coder chat` or
`mercury omp -p coder`. In the managed MIRC gateway room, use
`!spawn reviewer -p coder` or `!spawnomp reviewer -p coder`. Delegated code writers
follow the repository's worktree rules; do not share an editing checkout across
concurrent workers.

## Detailed references

Load the relevant reference when its details matter; verify uncertain behavior
against the current fork rather than inferring it from an upstream manual.

| Task | Reference |
|---|---|
| CLI commands and flags | [cli-reference.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/cli-reference.md) |
| In-session commands | [slash-commands.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/slash-commands.md) |
| Profiles and prompt isolation | [profiles.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/profiles.md) |
| Providers and model aliases | [providers-and-models.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/providers-and-models.md) |
| Engine configuration, toolsets, voice | [configuration.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/configuration.md) |
| Profile and project instructions | [project-context-files.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/project-context-files.md) |
| Permissions and privacy | [security-privacy.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/security-privacy.md) |
| Shared skills and engine tool mappings | [engine-tools.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/engine-tools.md) |
| Delegation, cron, curator | [background-systems.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/background-systems.md) |
| MCP servers | [native-mcp.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/native-mcp.md) |
| Webhooks | [webhooks.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/webhooks.md) |
| Themes | [themes.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/themes.md) |
| Desktop extensions | [desktop-plugins.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/desktop-plugins.md) |
| TUI widgets and pets | [tui-widgets.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/tui-widgets.md), [petdex.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/petdex.md) |
| Troubleshooting | [troubleshooting.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/troubleshooting.md), [windows-quirks.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/windows-quirks.md) |
| Contributions | [contributor-guide.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/contributor-guide.md) |
| Nous Portal access by another app | [portal-auth-for-third-party-apps.md](https://github.com/fengwhang/mercury/blob/main/hermes/skills/autonomous-ai-agents/mercury-agent/references/portal-auth-for-third-party-apps.md) |

## Operating constraints

Keep system prompts stable within a conversation. Updated profile Markdown is
loaded for a new session; do not rewrite a running agent's cached prompt.
Put credentials in the secrets file and behavior in configuration. Use the
selected engine's actual tool schema: Hermes names such as `terminal` and
`read_file` are not universal omp tool names. Preserve upstream SDK identifiers
and provider IDs when they are the real integration contract.
