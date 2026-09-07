---
name: grill-me
description: Use when asked to /grill-me. Loads the grilling interview.
---

Load the grilling skill and follow it exactly: call skill_view(name='grilling'), then conduct the interview it describes on the idea, plan, or decision the user brings.

Start from whatever the user gives you, however loose. Producing the sharp version is the session's job. Write no files; the only output is the conversation itself.

*(Ported from mattpocock/skills, skills/productivity/grill-me, MIT license, with Mercury adaptations: skill_view replaces Claude Code's Skill tool; upstream marks this skill user-invoked-only via disable-model-invocation, which Mercury does not support, so the description carries that intent instead.)*
