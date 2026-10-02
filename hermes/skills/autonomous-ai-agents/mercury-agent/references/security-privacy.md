# Security & Privacy Toggles

Common "why is Hermes doing X to my output / tool calls / commands?" toggles — and the exact commands to change them. Most of these need a fresh session (`/reset` in chat, or start a new `hermes` invocation) because they're read once at startup.

### Secret redaction in tool output

Secret redaction is **on by default** — tool output (terminal stdout, `read_file`, web content, subagent summaries, etc.) is scanned for strings that look like API keys, tokens, and secrets before it enters the conversation context and logs. Leave it enabled for normal use:

```bash
mercury config set security.redact_secrets true       # keep enabled globally
```

**Restart required.** `security.redact_secrets` is snapshotted at import time — toggling it mid-session (e.g. via `export HERMES_REDACT_SECRETS=false` from a tool call) will NOT take effect for the running process. Tell the user to change it in config from a terminal, then start a new session. This is deliberate — it prevents an LLM from flipping the toggle on itself mid-task.

Disable only when you deliberately need raw credential-like strings for debugging or redactor development:
```bash
mercury config set security.redact_secrets false
```

### PII redaction in gateway messages

Separate from secret redaction. When enabled, the gateway hashes user IDs and strips phone numbers from the session context before it reaches the model:

```bash
mercury config set privacy.redact_pii true    # enable
mercury config set privacy.redact_pii false   # disable (default)
```

### Command approval prompts

Mercury configures the two engines separately. Run `mercury setup approvals`,
`mercury setup hermes-approvals`, or `mercury setup omp-approvals`.

| Engine | Modes | Behavior |
|---|---|---|
| Hermes | `safe`, `smart`, `yolo` | Safe prompts for flagged shell commands; smart uses Hermes's auxiliary risk reviewer to approve, deny, or escalate; yolo bypasses recoverable prompts. Legacy `manual`/`off` aliases remain accepted. |
| omp | `always-ask`, `write`, `yolo` | Native tiers allow reads, or reads plus workspace writes, respectively; execution prompts remain in both restricted modes. Yolo bypasses recoverable native prompts. |

```bash
mercury config set approvals.mode smart
mercury config set omp.tools.approvalMode write
```

Hermes smart is not an omp policy. A harmless command need not trigger Hermes's
reviewer. Changing one engine's mode does not change the other. Child approvals
reach the orchestrator even across engine boundaries; respond with `!approve` /
`!deny` in MIRC or `/approve` / `/deny` on slash-command surfaces. Explicit deny
rules and provider safety confirmations remain effective under yolo.

Secret redaction is independent of approval mode.

### "Reset permissions" / "make Hermes ask again"

The user usually means: wipe the accumulated "Always allow" state — NOT yolo
mode, and NOT a per-edit diff prompt on the Hermes side; omp file writes follow its native tool tiers. Two stores hold it:

1. Shell-command allowlist: `mercury config set command_allowlist '[]'`
2. Shell-hook consent (only if present): `rm -f $HERMES_HOME/shell-hooks-allowlist.json`

Then sanity-check `mercury config get approvals.mode` (should not be `yolo` or its legacy alias `off`)
and confirm `--yolo` isn't baked into their launch alias or systemd unit.

### Shell hooks allowlist

Some shell-hook integrations require explicit allowlisting before they fire. Managed via `$HERMES_HOME/shell-hooks-allowlist.json` — prompted interactively the first time a hook wants to run.

### Disabling the web/browser/image-gen tools

To keep the model away from network or media tools entirely, open `mercury tools` and toggle per-platform. Takes effect on next session (`/reset`). See `references/configuration.md` for the toolset list.

