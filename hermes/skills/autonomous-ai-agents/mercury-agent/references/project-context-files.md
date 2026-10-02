# Profile and Project Context Files

Mercury composes profile instructions with repository instructions. Named profiles
own `config/SOUL.md`, `config/AGENTS.md`, `config/HERMES.md`, `config/OMP.md`,
`config/MEMORY.md`, and `config/USER.md` under their profile home; the default uses
`$MERCURY_HOME/config/`. See [profiles.md](profiles.md) for creation and cloning.

`SOUL.md` supplies identity. `AGENTS.md` applies across both engines;
`HERMES.md` applies to Hermes and `OMP.md` to omp. Use these files for instructions
that should follow this profile across projects. A missing file does not import
another profile's instructions; an empty canonical file suppresses its legacy copy.

## Hermes project discovery

Hermes also loads project context, using the first supported source it finds:

| Source | Discovery |
|---|---|
| `.hermes.md` / `HERMES.md` | Parent walk bounded by the git root |
| `AGENTS.md` / `agents.md` | Current working directory |
| `CLAUDE.md` / `claude.md` | Current working directory |
| `.cursorrules` / `.cursor/rules/*.mdc` | Current working directory |

The Hermes project context limit is 20,000 characters with head/tail truncation;
threat-pattern scanning applies before injection. These project discovery rules
are separate from always-loaded profile instructions. omp has its own project
instruction discovery; check `omp/packages/coding-agent/src/discovery/` rather
than assuming its rules match Hermes.

Keep repository build/style rules in the repository and personal working
preferences in the profile's `config/AGENTS.md`. Updated profile files load in a
new session. Check `mercury --help` for diagnostic flags such as `--ignore-rules`
and `--safe-mode`; they are not profile switching or permission controls.
