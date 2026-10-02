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
bundled Markdown defaults and initial model configuration. They do not read the
default profile's personal Markdown at runtime.

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
It is informational; create or configure profiles with the CLI commands above or
the profile creation controls, then launch a session using that profile.
