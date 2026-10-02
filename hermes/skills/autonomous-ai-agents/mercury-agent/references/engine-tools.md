# Using shared skills from either Mercury engine

The skills library is shared. A skill's workflow is independent of the engine
reading it; its tool-call examples may use one engine's syntax. Translate the
operation using tools actually exposed in the current session. Never invent
a missing tool or execute a harness tool call as Python or shell code.

| Operation | Mercury Hermes | Mercury OMP |
|---|---|---|
| Shell command | `terminal` | `bash` |
| Read a file | `read_file` | `read` |
| Find files or text | `search_files`, shell `rg` | `find`, `grep`, shell `rg` |
| Edit or write | `patch`, `write_file` | `edit`, `write` |
| Delegate | `delegate_task` | `task` |
| Load a skill | `skill_view` or available file tools | skill slash command or available file tools |

Check each live schema. Parameter names, time units, batch support, background
handles, and result envelopes differ. For example, put a shell snippet from
`terminal(command=...)` into the current `bash` command field; do not pass
Hermes-only keyword arguments to OMP.

## Delegation

Hermes uses `delegate_task` with its `goal`, `context`, and optional `toolsets`
fields. OMP uses the single `task` agent type; put the instructions and context
in its `task` text. A reviewer or investigator is a task description or child
name, never an additional model role.

An OMP single-child request, when its exposed schema offers this shape:

```json
{
  "name": "review-auth",
  "agent": "task",
  "task": "Independently review the supplied diff for logic and security issues. Return the requested JSON findings. Repository: /absolute/path. Diff: ..."
}
```

When OMP exposes batch mode, its request has a shared `context` string and a
`tasks` array of `name`, `agent: "task"`, and `task` entries. Do not copy
Hermes batch entries containing `goal` or `toolsets` into that array. Use
`outputSchema` only if the current schema offers it; parse and validate the
actual returned child output. A `task` response is not a `delegate_task`
response envelope.

Delegate only when permitted by the session and repository rules. Respect
current concurrency limits. If delegation or batching is unavailable, carry
out every requested review or investigation angle sequentially yourself and
state that limitation. Concurrent writers must use separate worktrees.

## Engine capabilities and permissions

Hermes toolsets and plugins are not automatically OMP tools. Browser, image,
voice, memory, and service workflows require their advertised integration
or documented CLI/MCP prerequisite. If a needed capability is unavailable,
use a supported alternative or report the missing prerequisite; do not pretend
the other engine's tool exists.

Keep the engines' permission settings distinct: Hermes safe/smart/yolo and OMP
always-ask/write/yolo. Shared skills do not change either policy or bypass
ancestor approval routing. Resolve configuration through `mercury config path`
and secrets through `mercury config env-path`, using `mercury-nightly` for a
nightly installation and preserving the active profile.
