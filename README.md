# Mercury 🌡️

**A persistent assistant, a dedicated coding engine, and a self-hosted place to watch and steer them.**

Mercury brings [Hermes](https://github.com/NousResearch/hermes-agent),
[Oh My Pi (OMP)](https://github.com/can1357/oh-my-pi), and a fork of
[The Lounge](https://github.com/thelounge/thelounge) into one distribution.
Hermes handles conversation, memory, skills, messaging, and scheduled work.
OMP handles coding with its native tools, language servers, debuggers, and
subagents. The **Observatory** gives those agents addressable MIRC rooms;
**mLounge** is its browser interface, our fork of The Lounge.
**MIRC** is our forked IRC layer: a custom server and Mercury extensions
for agent rooms, traces, and steering.

Ask your assistant to investigate a problem, let it hand the coding work
to OMP, open a child's room to see its progress, and send a correction
while it works. Use the terminal at your desk and the Observatory from
your phone. Your host runs the agents; you choose their supported model
providers.

The current stable release is **[v0.3.7](https://github.com/fengwhang/mercury/releases/tag/v0.3.7)**.
The standard installer below includes the same thinking kaomoji across Hermes
and OMP rooms, program-preserving steering, and shared approval settings.

## Why Mercury?

Mercury's contribution is the wiring between the three engines and surfaces:

- **An assistant that can hand work to a real coding runtime.** Hermes'
  `delegate_task` starts OMP children, collects their results, and brings
  them back into the conversation. The shipped instructions tell Hermes
  to delegate coding work. OMP children can delegate further.
- **Individual agents you can reach.** The Observatory creates rooms for
  spawned agents and delegated children. Follow tool activity, read a
  reply, steer a running RPC child, or stop it without asking the parent
  to relay every instruction.
- **Shared knowledge across two runtimes.** Both engines read the shared
  persona and instruction files and skills library. The bridge defaults
  OMP's memory to the shared Mnemosyne/Mnemopi SQLite bank, with text
  search enabled and embeddings optional. Children still have their own
  conversations; shared memory does not give them the parent's transcript.
- **One place to configure the handoff.** `config.yaml` contains the chat
  and coding model slots, optional fallback chains, thinking levels, and
  approval settings. Supported API-key credentials and compatible OAuth
  logins are shared between Hermes and OMP within the active profile.
- **Direct execution when you already know the task.** `mercury omp`
  opens OMP's TUI; `/omp` sends a task directly to OMP; `omp_direct` cron
  jobs run the coding engine without an intervening Hermes agent turn.
- **A browser surface you host.** mLounge, MIRC server, agent state, and
  uploaded artifacts live on your machine. We recommend Tailscale to
  reach the Observatory across devices. Model requests still go to the
  provider you configure, including a local endpoint where supported.

### Compared with the tools you already use

| Tool | What it already offers | Why choose Mercury instead? |
| --- | --- | --- |
| [OpenClaw](https://docs.openclaw.ai/concepts/multi-agent) | A self-hosted assistant gateway with multiple agents and channel routing. | Choose the packaged Hermes → OMP handoff, shared memory and credentials, and agent rooms in mLounge. |
| [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/) | Persistent memory, skills, delegation, messaging, and scheduling. | Keep that assistant workflow while making OMP the delegation engine and adding the Observatory's live coding-agent rooms. |
| [Claude Code](https://code.claude.com/docs/en/agent-teams) | A coding workflow with subagents and agent teams; [Remote Control](https://code.claude.com/docs/en/remote-control) connects local sessions to web and mobile. | Run Mercury's assistant, coding workers, and browser interface on your own server, with separately configured chat and coding providers. |
| [Codex](https://learn.chatgpt.com/docs/agent-configuration/subagents) | Coding agents and subagents, [remote access](https://learn.chatgpt.com/docs/remote), and [scheduled tasks](https://learn.chatgpt.com/docs/automations). | Choose the Hermes/OMP runtime combination, its shared local knowledge, and a self-hosted MIRC/browser control surface. |
| [OMP](https://github.com/can1357/oh-my-pi) | A coding TUI with native tools, LSP, DAP, memory, and parallel subagents. | Put an ongoing Hermes assistant, messaging gateway, scheduler, and browser agent rooms around that coding engine. |

These are workflow differences, not claims that Mercury invented memory,
multi-agent work, scheduling, or remote control. The comparison was checked
against the linked project documentation on September 30, 2026. Mercury
vendors specific upstream versions; see [PINS.txt](PINS.txt).

## Install

The published bundles contain **Linux x64 and ARM64** OMP executables.
Use a Linux host, or WSL2 with the required Linux services enabled. Native
macOS and Windows release bundles are not currently published.

```bash
curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install.sh | bash
```

The installer sets up Python dependencies, installs the prebuilt runtimes,
and runs `mercury setup` for provider login, model selection, tools, and
gateway configuration. End users do not compile OMP or the mLounge. The
mLounge requires **Node.js 22 or newer and npm** for its runtime dependencies;
install those before configuring the browser surface if they are absent.

```bash
mercury              # Hermes assistant
mercury omp          # OMP coding TUI
mercury setup        # reconfigure either side through the shared wizard
mercury update       # update a release installation
```

The default home is `~/.mercury`; the command shim is installed under
`~/.local/bin`. If your shell cannot find `mercury`, add that directory
to your PATH or open a new shell. Installer options include `--skip-setup`,
`--non-interactive`, `--skip-browser`, `--skip-observatory`, `--skip-gateway`,
`--no-skills`, and `--dir PATH`.

For a nightly installation:

```bash
curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install-nightly.sh | bash
```

Stable is **[v0.3.7](https://github.com/fengwhang/mercury/releases/tag/v0.3.7)**.
The latest nightly is **[v0.3.11-nightly](https://github.com/fengwhang/mercury/releases/tag/v0.3.11-nightly)**.
The nightly installer selects the newest published prerelease. Stable remains
on its own update channel; nightly is selected explicitly by this wrapper or
`--channel nightly`.

Linux bundles are provided for both **glibc and musl**, on x64 and
ARM64. The installer and updater select the host's CPU and libc. Alpine needs
`libstdc++` and `libgcc` (`apk add libstdc++ libgcc`). NixOS needs its standard
Linux loader compatibility enabled (`programs.nix-ld.enable = true;`). The
prebuilt OMP runtime does not depend on the release host's Nix store paths.

This uses `mercury-nightly` and `~/.mercury-nightly`. State directories are
separate, but the Observatory service names and default ports currently
overlap: use one Observatory installation per Linux user. See the
[integration review](docs/code-review-2026-09-30.md) for current update and
configuration limitations.

## Observatory on your phone, tablet, and other computers

**Recommended: one Linux host running Mercury, with all your devices on
Tailscale.** The host keeps working while a browser disconnects. Each
device opens the same mLounge and its agent rooms.

Tailscale connects devices through a private network called a tailnet.
Install it on the Mercury host and each device you want to use, and sign
them into the same tailnet. Follow the official
[installation guide](https://tailscale.com/docs/install) for each platform.

### 1. Connect the host to Tailscale

On a Linux distribution supported by Tailscale's installer:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status
tailscale ip -4
```

Open the authentication URL printed by `tailscale up`. Save the host's
Tailscale IPv4 address, such as `100.101.102.103`. For NixOS or other
distributions with their own package setup, use Tailscale's
[Linux instructions](https://tailscale.com/docs/install/linux).

### 2. Install Mercury and configure its gateway

Run the Mercury installer above as your normal Linux user. Complete
provider login and choose the chat and delegate models. Enable the
gateway when offered. If you skipped that section, run:

```bash
mercury setup gateway
```

Keep Mercury running on the host that holds your projects and tools.

### 3. Enable the Observatory and its browser interface

```bash
mercury setup observatory
```

In the wizard:

1. Enable the Observatory and choose an MIRC network name.
2. Choose the Tailscale address when offered a bind for the client-facing
   MIRC server. The internal agent listener stays on localhost.
3. Accept the offer to install the mLounge.
4. For **“Pin mLounge to Tailscale or localhost?”**, choose **Tailscale**.
   The MIRC bind and the mLounge web bind are separate choices.
5. Choose a mLounge username and password, and save the printed login card.
   This browser password is separate from the MIRC server password.
6. Accept the service starts/restarts offered by setup.

On a fresh setup, Mercury seeds the mLounge's connection to the MIRC server
and joins `#<network>_gateway`. The default web port is **9000**; use the
actual URL from the login card if your configuration differs.

Print that login card again whenever you need it:

```bash
mercury observatory login
# Nightly installation:
mercury-nightly observatory login
```

This shows the web address, mLounge username, Tailscale guidance, and
connection details for adding this machine to another mLounge.
When the web UI is bound to Tailscale, the URL prefers its MagicDNS name
and falls back to the Tailscale IP if no name is available.
It does not reset your password; the mLounge password
is only shown when created or reset.

### 4. Open the web UI from another device

Install and connect the Tailscale app on your phone, tablet, or computer.
In its browser, open the host address from the login card, for example:

```text
http://mercury-host.example-tailnet.ts.net:9000
```

Log in with your **mLounge** credentials. Open `#<network>_gateway` and send
a message. You should see an agent response in that room. You can now
watch work and send instructions from any connected device with access to
the host. Access follows your tailnet policy and the host's firewall;
no router port forwarding is needed for this setup.

### 5. Keep it available and check the connection

For a Linux host with systemd, allow your user services to keep running
after logout:

```bash
sudo loginctl enable-linger "$USER"
mercury observatory status
mercury observatory doctor
mercury observatory rooms
```

The host must stay awake, online, and connected to Tailscale. WSL2 also
needs its Linux environment and systemd services running.

If the page will not load, check that both devices are connected to the
same tailnet, that its policy permits the host's web port, and that the
mLounge is listening on the Tailscale address. On the host:

```bash
systemctl --user status mercury-observatory.service mercury-lounge.service
journalctl --user -u mercury-lounge.service -n 50
```

If an existing mLounge was configured for localhost, setup keeps that
installation and its bind. Change `host` in
`~/.mercury/observatory/lounge/home/config.js` to the host's Tailscale IPv4
address, then run `systemctl --user restart mercury-lounge.service`.
Use the corresponding home if you changed `MERCURY_HOME`.

If the page loads but the agent does not answer, run
`mercury observatory doctor`, check `mercury gateway status`, and use
`mercury observatory restart` to restart and verify the chat path.
Sending `!restart` in the managed MIRC gateway room also restarts the full
Observatory. It restarts MIRC and the gateway, and refreshes mLounge when installed.
Observatory restarts checkpoint active gateway sessions and resume them,
without waiting for a long model turn to finish. mLounge is optional for
this command.

Spawned agent sessions and completed subagent rooms stay in the Observatory
until you send `!exit` in their room. Closing a parent also closes its
descendant rooms. A transport reconnect retains the sessions and restores
room membership; idle agent identities reconnect automatically.
Re-run `mercury setup observatory` if you need to reset a lost mLounge
password.

### Connect mLounge to another machine's MIRC server

One mLounge can stay connected to several Mercury machines. Suppose
machine **A** runs the mLounge you use, and machine **B** runs the agents
you want to reach. Connect both machines to the same Tailscale tailnet.

1. On **B**, run `mercury setup observatory` and bind the client-facing
   **MIRC server** to B's Tailscale address. Binding only the web UI does
   not expose MIRC to A.
2. On **B**, run `mercury observatory login`. The **“this box from another
   mLounge”** section prints B's connection details. It prefers B's
   MagicDNS name and falls back to its Tailscale IP.
3. Open **A's mLounge** in your browser and choose **Connect / Add network**.
   Fill in B's details:

   | Field | Value |
   | --- | --- |
   | Network name | A label for B, such as `mercury-b`. |
   | Server / host | B's MagicDNS hostname from the card, without `http://`. |
   | Port | B's MIRC server port, normally **6670**, rather than web port 9000. |
   | TLS | **Off** for the plaintext MIRC listener over Tailscale. |
   | Server password | `IRC_CLIENT_PASSWORD` from **B's** Mercury home `.env`. |
   | Nickname | A unique name for your connection, separate from agent nicks. |
   | Channels | `#<B-network>_gateway`, as printed on B's card. |

4. Connect and send a message in B's gateway room. Add another network
   for each Mercury machine you want to monitor.

Your mLounge web password logs you into A's browser interface; each MIRC
network has its own server password. The login card points to the file
holding that password without printing the secret. B needs its MIRC and
gateway services running; it does not need its own mLounge web UI for A
to connect. Allow A to reach B's MIRC port in the tailnet policy and B's
firewall. Use `mercury-nightly` for the same commands on a nightly install.

The fork names are mLounge and MIRC. Existing service names
(`mercury-lounge.service`, `mercury-observatory.service`), `lounge/` paths,
and `IRC_*` configuration keys keep their names for compatibility.

## Working in agent rooms

| Action | What happens |
| --- | --- |
| Chat in `#<network>_gateway` | Talk to the Hermes gateway assistant. |
| `/spawn <name>` | Create another Hermes agent room. |
| `/spawnomp <name>` | Create an OMP coding-agent room. |
| Message a running RPC child room | Send guidance to that child. |
| `/stop` | Interrupt work in the room. |
| `/exit` in a spawned room | Stop the spawned agent and remove its room. |

Agent replies support Markdown and LaTeX. Tool inputs, tool outputs,
thinking traces, and status events render as plaintext. Inline and fenced
code stay literal, so shell variables, underscores, and globs survive
rendering. **Raw** toggles the original message text for manual selection.
Agents can share local artifacts through the mLounge's upload links.

Room control depends on the running transport: RPC children accept
steering; legacy one-shot children provide traces and stop control.
The browser supports the gateway and OMP room commands, rather than every
terminal-only interactive screen.

## Configuration and state

```text
~/.mercury/
├── config.yaml          shared model slots and engine settings
├── .env                 shared environment credentials
├── config/              SOUL, MEMORY, USER, AGENTS, HERMES, and OMP markdown
├── skills/              shared skill library
├── memories/            shared memory bank
├── hermes/              Hermes profile, authentication, and runtime state
├── omp/                 OMP runtime state
├── observatory/         MIRC state, mLounge configuration, and uploads
└── mercury-agent/       installed source and prebuilt runtime bundles
```

Named profiles live in `~/.mercury/hermes/profiles/<name>/`. Each owns a
`config.yaml` and `config/` with `SOUL.md`, `AGENTS.md`, `HERMES.md`,
`OMP.md`, `MEMORY.md`, and `USER.md`. Both engines build that profile's
system prompt from its own files; missing files never inherit the default
profile's persona or instructions. Existing profile Markdown migrates locally.
Cloning copies instructions once, so subsequent edits stay independent;
export/import and the profile editors use the same layout.

```bash
mercury profile create coder
mercury -p coder setup
mercury omp -p coder
```

In the gateway room, `!spawnomp reviewer -p coder` launches an OMP session
with that profile's model, reasoning, native permissions, and instructions.
The profile survives Observatory restarts. For OMP's native short print
flag, use `mercury omp -p coder -- -p 'your prompt'` or `--print`.
The nightly command supports the same options.

The shared configuration has `models:`, `approvals:`, `hermes:`, and `omp:`
sections. `models.default` selects the Hermes chat model;
`models.delegate_model` selects the OMP model, including `mercury omp`.
Both primary fallback slots and their ordered chains are optional.
Setup selects four models in order: default, fallback, delegate, and delegate
fallback. Each can use its own provider, with reasoning and then context-window
selection immediately after its model selection. Configure additional retry models by hand in
`models.fallback_chain` and `models.delegate_fallback_chain`, starting each
chain with its primary fallback. Setup preserves those extra entries unless
you skip their primary fallback; entries duplicating a newly selected model
are removed. Menus use that provider's per-model API metadata and
respect mandatory reasoning. If no effort choices are published or available,
setup keeps the current setting and explains why.
Selections are stored in `models.reasoning_overrides` and applied to both
engines' fallback chains.
Both engines use advertised effort levels for runtime requests as well:
live capability metadata overrides bundled model rules, mandatory reasoning
stays enabled, and models without an effort selector keep their provider's
reasoning controls without receiving an invented effort tier. Codex discovery
retains the account's supported levels and default. When discovery is
unavailable, existing compatibility rules remain the offline fallback.
Context selection uses the serving provider's advertised default and maximum.
Choose Default, Maximum (when larger), or Custom; a provider advertising one
window gets Default and Custom. Custom limits cannot exceed an advertised
maximum. If metadata is unavailable, keep automatic detection or enter a known
limit. Larger windows may incur premium pricing or consume more subscription
credits. Choices are stored in `models.context_windows`, keyed by
`provider/model`, and apply to both engines, including fallback models.

Built-in context compaction is enabled by default, including Blank Slate.
The Context Engine Plugin Tools checkbox controls optional plugin tools;
it does not enable or disable the built-in compressor. Run
`mercury setup context` to choose 50%, 75%, or a custom compaction percentage
for both engines. Setup removes older token caps and model-specific overrides
so the selected percentage takes effect; output headroom still bounds the
trigger near a provider's limit. Named profiles keep their own settings.

Use `mercury setup model` for provider-aware selection and
`mercury omp-sync` after hand-editing shared settings.

```yaml
models:
  default: your-provider/chat-model
  delegate_model: your-provider/coding-model
  orchestrator_thinking_level: high
  delegate_thinking_level: xhigh
  context_windows:
    your-provider/chat-model: 200000
    your-provider/coding-model: 400000
hermes:
  compression:
    enabled: true
    threshold: 0.75
    respect_threshold_percent: true

approvals:
  mode: smart # Hermes: safe | smart | yolo (manual/off remain accepted)
omp:
  tools:
    approvalMode: write # OMP: always-ask | write | yolo
```

OMP has one model role, **task**. Workers use that task model and fallback
chain; legacy role assignments cannot reroute them.

Configure approval options separately with `mercury setup approvals`, or use
`mercury setup hermes-approvals` and `mercury setup omp-approvals` for one engine.
Hermes safe mode asks before flagged shell commands; smart mode uses its LLM
risk reviewer to approve, deny, or escalate those commands. OMP uses native tool
tiers: always-ask allows reads, write also allows workspace writes, and both
ask before execution. OMP does not use Hermes's smart reviewer.

Each engine reads its own mode. OMP descendants inherit the live OMP policy;
explicit deny rules remain shared. Nested approval requests reach the orchestrator's TUI
or its mLounge room, even when a Hermes parent uses YOLO and an OMP child uses write.
Reply `!approve` / `!deny` in mLounge, or `/approve` /
`/deny` on slash-command surfaces. Background children keep that route
available after the parent finishes its turn. Explicit deny rules and
provider safety confirmations still apply under YOLO.

YOLO bypasses recoverable approval prompts within the engine configured for it,
including OMP per-tool prompt rules. Changing one engine's mode leaves the other unchanged.
In immediate steering mode, a message to a working OMP agent interrupts its
model output and continues the same run with your correction. Tracked shell
commands yield as background jobs and deliver their results later;
steering never kills them. Tools that cannot safely yield finish before the
correction is injected. Explicit stop and cancel actions remain separate.

The bridge preserves independent OMP settings when it updates the shared
policy. See the [code review](docs/code-review-2026-09-30.md) for remaining
integration limitations.

Coming from an existing installation? Preview the import first:

```bash
mercury migrate-hermes --dry-run
mercury migrate-omp --dry-run
```

Then run the appropriate command without `--dry-run` to choose what to
import. Hermes migration covers persona, memory, settings, credentials,
skills, history, and cron jobs. OMP migration covers sessions, custom
models, selected settings, MCP/SSH definitions, and themes.

## Development

The engines are vendored and patched. [PINS.txt](PINS.txt) records their
upstream bases; consult the applicable `AGENTS.md` before changing them.
The integration lives in `bridge/`, `hermes/tools/omp_*`,
`hermes/mercury_cli/`, `hermes/observatory/`, and `third_party/mlounge/`.

Implementation names use **mLounge** and **MIRC**. Historical `IRC_*` keys,
transport IDs, state directories, and installed service names remain compatibility
interfaces so upgrades preserve existing accounts and rooms. Upstream credits
and actual IRC protocol/dependency names retain their original names.

Run Python checks through `hermes/scripts/run_tests.sh`, OMP checks through
Bun, and mLounge checks through Vitest. The
[September 2026 review](docs/code-review-2026-09-30.md) records the reviewed
paths, reproducible findings, and current test results.

Release builds must bump and commit the Mercury version **before**
compiling OMP. Build both OMP architectures and the Hermes TUI, run
`scripts/build-mlounge-fork.sh`, then `scripts/make-dist.sh`. Packaging
checks the baked OMP version, native-library version, and mLounge source
fingerprint. Published bundles include the prebuilt components.

## Credits and licenses

Mercury builds on [Hermes by Nous Research](https://github.com/NousResearch/hermes-agent),
[OMP by can1357](https://github.com/can1357/oh-my-pi), and
[The Lounge](https://github.com/thelounge/thelounge), each licensed under
MIT. Their tools and capabilities belong to their respective upstream
projects; Mercury maintains the distribution and its integration patches.
See the license files in each vendored tree.
