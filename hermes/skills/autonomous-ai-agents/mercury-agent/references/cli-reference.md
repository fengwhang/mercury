# Mercury CLI Reference

Use `mercury-nightly` for a nightly installation. Mercury source and help take
precedence over upstream documentation. Live sources when anything looks stale: `mercury --help`, `mercury <command> --help`,
https://hermes-agent.nousresearch.com/docs/reference/cli-commands

### Global Flags

```
mercury [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
mercury chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
mercury setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
mercury model                Interactive model/provider picker
mercury fallback [add|remove|list]  Fallback provider chain
mercury config [show|edit|get|set|unset|path|env-path|check|migrate]
mercury login / logout       OAuth sign-in / clear stored auth
mercury doctor [--fix]       Check dependencies and config
mercury status [--all]       Component status
```

### Tools & Skills

```
mercury tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

mercury skills list|browse|search QUERY|inspect ID
mercury skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
mercury skills config        Enable/disable skills per platform
mercury skills check|update|uninstall|publish PATH
mercury skills tap add REPO  Add a GitHub repo as a skill source
mercury bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
mercury mcp add NAME (--url or --command) | remove | list | test NAME
mercury mcp catalog | install NAME     Curated catalog install
mercury mcp configure NAME             Toggle tool selection
mercury mcp serve                      Run Hermes as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
mercury gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `mercury photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/

### Sessions

```
mercury sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
mercury cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
mercury webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
mercury profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
mercury profile rename A B | alias NAME | export NAME | import FILE
```

Named profiles own their instruction folder; see [profiles.md](profiles.md).
`mercury omp -p NAME` launches the coding engine with the same profile.

### Observatory

```
mercury setup observatory
mercury observatory login|doctor|restart
mercury omp [-p NAME] [-- NATIVE_ARGS]
```

### Credentials & Pools

```
mercury auth                 Interactive credential manager
mercury auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
mercury auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
mercury desktop / gui        Native desktop app
mercury dashboard            Web admin panel + embedded chat (--stop / --status)
mercury proxy                OpenAI-compatible local proxy backed by an OAuth provider
mercury portal               Quick setup / sign in via Nous Portal
mercury project              Named multi-folder workspaces
mercury skin list|use|set    Switch/tweak skins (see references/themes.md)
mercury pets <verb>          Pet mascots (see references/petdex.md)
mercury memory setup|status|off|reset   Memory provider
mercury secrets bitwarden|onepassword   External secret stores
mercury moa                  Mixture-of-Agents slots
mercury hooks / security / backup / import / checkpoints / console
mercury logs [-f] [errors]   View agent/error logs
mercury send                 One-off message through a gateway platform
mercury pairing / plugins / insights / journey / computer-use
mercury acp                  ACP server (IDE integration)
mercury completion bash|zsh|fish
mercury update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `mercury photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `mercury config edit` · [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) |
| Tools / toolsets | `mercury tools list` · [Tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference) |
| Skills catalog | `mercury skills browse` · [Skills catalog](https://hermes-agent.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `mercury model` · [Providers guide](https://hermes-agent.nousresearch.com/docs/integrations/providers) |
| Env variables | `mercury config env-path` · [Env vars reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) |
| Gateway logs | `$HERMES_HOME/logs/gateway.log` (or `mercury logs`) |
| Sessions | `mercury sessions browse` (reads state.db) |
