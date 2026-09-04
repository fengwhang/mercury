# AGENTS.md — shared working instructions (BOTH engines read this)
# Lives at $MERCURY_HOME/AGENTS.md after first boot. Unlike SOUL.md (persona)
# and unlike HERMES.md/OMP.md (per-side supplements), this file carries
# project-independent WORKING RULES both halves of Mercury must follow.
# Project-level AGENTS.md files (in a repo's cwd) still load as usual and
# are additive to this one. Edit freely — updates never overwrite.

## Standing rules for Mercury

- Coding tasks always delegate to omp subagents (see HERMES.md for the
  full policy). This file overrides any contrary instruction from an
  upstream default.
- Never write secrets into the repo; keys live in .env files outside it.
- Prefer systemic fixes over output patches: fix the generating job or
  script, not just the artifact it produced.
- When a task touches both engines, remember state is shared at
  ~/.mercury (SOUL/MEMORY/USER/skills) and private under
  ~/.mercury/hermes and ~/.mercury/omp.
