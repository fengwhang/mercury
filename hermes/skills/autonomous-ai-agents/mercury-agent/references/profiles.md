# Mercury Profiles

Use the same installation command throughout: `mercury` for stable or
`mercury-nightly` for nightly. Profiles belong to that installation's home.

```bash
mercury profile create coder
mercury -p coder setup
mercury profile show coder
mercury -p coder chat
mercury omp -p coder
```

Names use lowercase letters, digits, hyphens, and underscores; they are single
arguments, not space-separated display names. Fresh profiles receive their own
bundled Markdown defaults and inherit main-profile inference defaults. They do not read the
default profile's personal Markdown at runtime.

Models, fallbacks, reasoning levels and context budgets inherit live from the
main profile; local `models:` entries override selected defaults. Provider API
keys and OAuth logins inherit from the same installation. OAuth refreshes write
to the main login owner, so Hermes and OMP do not duplicate rotating grants.
To use independent models or logins, set `profile.inherit_models: false` or
`profile.inherit_credentials: false` in the named profile's `config.yaml`.
Older explicit model selections remain overrides; remove them to inherit.
Prompts, sessions, native approvals and messaging credentials remain local.

Each named profile owns `$MERCURY_HOME/hermes/profiles/<name>/config/`:

| File | Purpose |
|---|---|
| `SOUL.md` | Profile identity and persona, used by both engines |
| `AGENTS.md` | Shared profile instructions for both engines |
| `HERMES.md` | Hermes engine instructions |
| `OMP.md` | omp engine instructions |
| `MEMORY.md`, `USER.md` | Profile's Markdown memory and user context |

These files compose the profile's system prompt, along with its selected engine's
base prompt and project instructions. Edit this folder, not a sibling/default
profile. Explicitly empty files remain empty. Existing profile-local legacy files
are copied into this folder when migrated; canonical files take precedence.

```bash
mercury profile create reviewer --clone-from coder
mercury profile create backup --clone-all
mercury profile export coder -o coder.tar.gz
mercury profile import coder.tar.gz --name coder-copy
```

`--clone` copies the active profile's configuration, secrets, Markdown, and skills;
`--clone-from` selects the source. `--clone-all` copies additional state with the
CLI's history exclusions. Markdown copies and included files are independent,
including when the source used symlinks. Imports and distributions use the same
canonical prompt folder. The shared skills library and external memory backend
have their own scoping; owning Markdown files does not imply separate databases.

In the managed MIRC gateway room:

```text
!spawn assistant -p coder
!spawnomp reviewer -p coder
```

The agent name and profile name are separate arguments. omp children use the
profile's model, effort, fallback, native approval policy, and prompt files on
spawn and resume. For native omp print mode, preserve the argument boundary:

```bash
mercury omp -p coder -- -p "Summarize this repository"
```

`/profile` displays the profile serving the current chat and its prompt directory.
In a Hermes session, `/profileadd coder` creates a fresh profile just like
`mercury profile create coder`, including its config, independent Markdown files,
bundled skills, and a command alias when available. It uses the current
installation's home: stable and nightly profiles stay separate. It leaves the
current session in its existing profile. Configure and launch the new profile
with the commands shown in the reply.
