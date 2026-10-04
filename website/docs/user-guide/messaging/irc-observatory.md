# Observatory: mLounge and MIRC

Observatory gives Mercury agents persistent, addressable chat rooms. Open
**mLounge**, Mercury's fork of The Lounge, in a browser to talk to Hermes and
OMP agents, watch their work, and answer approval requests. **MIRC** is
Mercury's fork of the IRC transport, with agent routing and typed messages.
Use mLounge for the supported browser workflow.

## Set up browser access across devices

1. Install Mercury on the machine that will run your agents.
2. Install [Tailscale](https://tailscale.com/docs/install) on that machine and
   your phone, tablet, or other computer. Sign them into the same tailnet.
3. Run `mercury setup observatory` (or `mercury-nightly setup observatory`).
   Provision the MIRC server and mLounge, and accept the wizard's Tailscale
   binding choices. The server listener and browser listener are separate
   choices; bind both when another Mercury machine will connect to this one.
4. Read the final login card. Open its mLounge URL on any device in your
   tailnet and sign in with the **mLounge account** created by setup. Its
   password differs from the MIRC server password. When Tailscale is bound,
   the card prefers the machine's MagicDNS name and includes the web port
   (normally `9000`).

Print the card again whenever needed:

```sh
mercury observatory login
# For a nightly installation:
mercury-nightly observatory login
```

A forgotten browser password can be reset by re-running setup. The login
command prints connection instructions; it cannot recover a hashed password.
On Linux, setup offers systemd user services for persistent availability. Use
`mercury observatory doctor` to diagnose binding, connection, and service
problems. A machine that is asleep or powered off cannot serve its web UI.

Use one Observatory installation per Linux user: stable and nightly currently
share service names and default ports.

## One mLounge, multiple Mercury machines

An mLounge instance on machine A can stay connected to the MIRC server on
machine B. Both machines must be on the same tailnet.

1. On B, run `mercury setup observatory` and bind its **server listener** to
   Tailscale. Run `mercury observatory login` to display B's remote-network
   connection instructions.
2. Open A's mLounge in a browser and add a network. Enter B's MagicDNS name
   in **Server**, and the server port shown by B's card in **Port**. Keep the
   host and port in separate fields. For the ordinary tailnet listener,
   the default is `6670` with TLS off; Tailscale encrypts the connection.
3. Use B's server password from B's `~/.mercury/.env`
   (`IRC_BOUNCER_PASSWORD`, an existing compatibility key), choose a nickname,
   and join B's gateway room, normally `#<server-name>_gateway`.

The mLounge browser password logs you into A's web UI. B's MIRC server
password connects that network. They are separate credentials. If B uses the
TLS listener, use the card's TLS port and configure trust for its certificate.

Keep both networks in the same mLounge account to switch between their rooms.
There is no need to run a separate browser frontend on every agent machine.

## Rooms and commands

The gateway room hosts the main Hermes conversation. In that room, use:

- `!spawn <name>` or `/spawn <name>` to create a Hermes agent room.
- `!spawnomp <name>` or `/spawnomp <name>` to create an OMP agent room.

Each spawned agent has its own room. Delegated work appears in child trace
rooms. Hermes thinking traces are hidden by default; enabling
`display.thinking_progress` shows them. Thinking traces, tool inputs, and tool outputs display as plaintext;
assistant replies use Markdown and LaTeX. The **Raw** option exposes the
original message for selection and copying. Put copyable shell commands in
code fences so punctuation remains literal in a formatted reply.

Chat also shows the engines' user-facing progress: tool completion labels,
full todo lists, delegation completion, compaction, retries, fallback
changes and extension notices.

The gateway and explicitly spawned agents are level 0. A child of a level-N
agent is level N+1. Level-1 agents report to their parent and close when
their task completes, taking their descendants with them. Deeper agents
report when their task completes and remain available until their parent
ends. Closing a room ejects its users and removes it from mLounge, including
other connected browsers. Parent reports continue through the family until
the level-0 agent's task finishes.

In an OMP room, `!model` reports the current model and
`!model PROVIDER/MODEL` switches it through the engine's local command
handler. These commands remain available during a turn and when the
provider has exhausted its usage allowance.

Steer an agent by writing in its room. Delivery occurs at the engine's next
supported boundary; it does not guarantee interruption of a running tool.
Use `!stop` to request cancellation and `!exit` to close a spawned room and
its descendants. The gateway room cannot be exited this way.

When any descendant needs human approval, its request reaches the ancestor's
approval UI, including across Hermes and OMP. Reply `!approve` / `!deny` (or
`/approve` / `/deny`) in the room displaying the prompt. Hermes uses
**safe**, **smart**, or **yolo**; OMP uses **always-ask**, **write**, or
**yolo**. Each engine keeps its configured policy, with approval requests
routed up the family to the level-0 user interface. OMP uses one model role, **task**, with its configured fallback
chain; agents cannot assign separate model roles to their children.

MIRC retains a bounded message history. Agent state and local transcripts
have their own persistence; browser scrollback is not a complete transcript
archive. Existing `IRC_*` environment keys, `ircd.json`, and older service
names are kept for compatibility with installed configurations.
