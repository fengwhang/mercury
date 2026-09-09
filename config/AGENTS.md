# AGENTS.md — TOP LEVEL IMPERATIVES (BOTH engines read this)

- ALWAYS pass detailed technical plans, designs, and specifications to subagents; absolutely do NOT assume they already understand your intent. Subagents do NOT automatically inherit parent context windows.
- A complex task that can be broken down into smaller parts should have one subagent dispatched to tackle each part; parallel > serial. 
- Each subagent gets their own branch in the repo, for the orchestrator to later merge.
- Be PERSISTENT and INDEPENDENT in your task! Complete each next step as described; do not pause for user input unless it is absolutely necessary.
- NEVER write secrets into the repo; keys live in .env files outside it.
- Prefer systemic fixes over output patches: fix the generating job or
  script, not just the artifact it produced.
