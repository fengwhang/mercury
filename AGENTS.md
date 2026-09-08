# AGENTS.md — Mercury repo working rules (all agents read this)

## Standing rule: subagents get their own git worktrees + branches (2026-09-08)

On 2026-09-08 a subagent's git cleanup wiped hours of uncommitted sibling
work from the shared checkout (tracked mods reverted, untracked tests
deleted — see docs/design/matrix-observatory.md recovery notes). Root
cause: ten concurrent writers sharing one working tree with nothing
committed. This rule prevents that class of loss.

**Every subagent that modifies files works in its OWN git worktree on its
OWN branch — never in the shared checkout's working tree.**

1. **Before editing**, from the repo root, the parent (or the subagent's
   first step) creates a worktree:
   `git worktree add ../mercury-worktrees/<wave>-<area> -b agent/<wave>-<area>`
   (e.g. `agent/m4-approvals`). The subagent's cwd IS its worktree.
   **Branch lineage mirrors the delegation tree:**
   - A subagent's branch branches off its PARENT's current branch (the
     branch the parent itself is working on). Grandchildren branch off
     children's branches, and so on — so a parent integrates its own
     children before presenting its branch upward.
   - SIBLINGS branch from the same base point (their common parent's
     branch as of wave dispatch) — never from each other. Sibling B must
     not silently build on unverified sibling A work; the orchestrator
     keeps the ability to reject A's branch independently.
   - **Never branch from stale `main` when an integration branch is
     active**: if the wave's integration branch is
     `recovery/matrix-observatory` (or any `agent/`-integration branch),
     worktrees branch from THAT branch's head, not from `main`. Check
     `git branch --show-current` in the shared checkout if unsure.
2. **Commit early, commit often** in your own branch — after every
   verified slice (tests green), not at the end. Uncommitted work does
   not exist.
3. **Integration is the orchestrator's job, not a sibling's.** When a
   subagent's slice is verified, the orchestrator merges its branch into
   the integration branch (`git merge --no-ff agent/<...>` from the
   integration checkout) and resolves any conflicts itself. Siblings
   never merge, rebase, or touch each other's branches.
4. **Destructive git is confined to your own worktree** — and even there,
   `checkout -- .` / `clean` / `reset --hard` / `stash -u` are last
   resorts. They are FORBIDDEN in the shared checkout and in any
   `../mercury-worktrees/*` directory you do not own.
5. **Read-only work** (exploration, transcript mining, test runs) may use
   the shared checkout — reading is always safe.
6. **Worktrees are removed by the orchestrator** after merge:
   `git worktree remove ../mercury-worktrees/<name> && git branch -d ...`.

Shared-checkout exception: a SINGLE writer (e.g. a recovery orchestrator)
may work and commit in the shared checkout directly, on a non-main
branch, committing per verified area.
