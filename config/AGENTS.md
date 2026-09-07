# AGENTS.md — shared working imperatives (BOTH engines read this)

- TOP PRIORITY: Always pass detailed technical plans, designs, and specifications to subagents; absolutely do NOT assume they already understand your intent. Subagents do NOT automatically inherit parent context windows.
- Never write secrets into the repo; keys live in .env files outside it.
- Prefer systemic fixes over output patches: fix the generating job or
  script, not just the artifact it produced.
- When a task touches both engines, remember state is shared at
  ~/.mercury/config (SOUL/MEMORY/USER/skills) and private under
  ~/.mercury/hermes and ~/.mercury/omp.
