# Mercury Profiles

Use the same installation command throughout: `mercury` for stable or
`mercury-nightly` for nightly. Profiles belong to that installation's home.

```bash
mercury profile create coder
mercury profile models coder  # optional four-slot override
mercury profile show coder
mercury -p coder chat
mercury omp -p coder
```

Names use lowercase letters, digits, hyphens, and underscores; they are single
arguments, not space-separated display names. Fresh profiles receive their own
bundled Markdown defaults and inherit main-profile inference defaults. They do not read the
default profile's personal Markdown at runtime.

Models, fallbacks, reasoning levels and context budgets inherit live from the
main profile. New profiles have no model override. Use
`mercury profile models NAME` (or `mercury-nightly profile models NAME`) for the
setup model picker, including reasoning and context immediately after each of
four selections: default, fallback, delegate, delegate fallback. Overrides live
under `profile_models.NAME` in the installation's main `config.yaml` and apply
to both engines. The picker commits the complete entry only when finished;
cancellation leaves settings unchanged. Optional fallbacks may be empty.
Explicit entries are absolute: invalid settings raise an error instead of
borrowing main-model slots or silently selecting a main model. Use
`mercury profile models NAME --inherit` to remove an override. Legacy local
`models:` and `profile.inherit_models` no longer control model selection.
Provider API
keys and OAuth logins inherit from the same installation. OAuth refreshes write
to the main login owner, so Hermes and OMP do not duplicate rotating grants.
To use independent logins, set `profile.inherit_credentials: false` in the
named profile's `config.yaml`. Model overrides remain separate from credentials.
Clone, rename, export/import and delete preserve or remove the matching central
entry; inheriting profiles remain unpinned.
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
canonical prompt folder. Mnemosyne/Mnemopi uses a separate
`memories/mnemopi.db` per profile, shared by Hermes and OMP in that profile.
Normal clones start with an empty bank; `--clone-all` snapshots the source
memories into an independent database. Existing pins outside a named profile
are rebased to its local bank. This does not change other memory providers'
external-service scoping. Both engines share the selected profile's skills
library. Fresh profiles receive stock Mercury skills; clones copy their source
profile's skills, including customizations and intentional removals.

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
