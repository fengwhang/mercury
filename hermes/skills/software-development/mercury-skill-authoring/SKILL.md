---
name: mercury-skill-authoring
description: "Author in-repo SKILL.md files: frontmatter and structure."
version: 3.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  mercury:
    tags: [skills, authoring, hermes-agent, conventions, skill-md]
    related_skills: [requesting-code-review]
---

# Authoring Mercury Skills

This established skill slug describes Mercury's source tree. Add bundled skills
to `hermes/skills/<category>/<name>/`, or optional integrations to
`hermes/optional-skills/<category>/<name>/`. Personal `skill_manage` operations
write to the installed shared library, not this repository.

## Workflow

1. Read the applicable `AGENTS.md` and inspect related skills before adding a
   sibling. Extend a suitable skill and consolidate duplicate slash names.
2. Keep YAML frontmatter at byte zero with `name` and a concise `description`;
   preserve human attribution, license, supported platforms, and real integration
   IDs. Existing `metadata.mercury` is the loader's metadata contract, not a
   statement that this is stock Hermes.
3. Put reusable decisions and required setup in `SKILL.md`. Move substantial
   conditional instructions into referenced files. A skill must have a useful
   workflow of its own rather than forwarding to a duplicate skill.
4. Use Mercury CLI commands and installation variables. Both engines share
   `$MERCURY_HOME/skills`; named profiles own their `config/*.md` prompt files.
   Use `mercury config path` / `env-path` to resolve settings and secrets. For
   nightly, use `mercury-nightly` throughout.
5. Match the engine's actual tools. Examples of Hermes `terminal`, `read_file`,
   or `skill_view` are not automatically omp tool names. Mercury delegation
   uses omp's sole `task` role. Do not reintroduce stock role routing, shared
   profile personas, or Hermes smart approvals into omp.
6. Validate frontmatter, names, platform requirements, reference links, and
   helper scripts. Run meaningful behavior tests through `hermes/scripts/run_tests.sh`
   with temporary homes; do not test copies of headings or prose. Regenerate
   skill catalog pages when names/frontmatter change and preserve unrelated work.
7. Review the diff. Commit, publish, or release only when the task authorizes it.

## Platform and security checks

Audit subprocesses and dependencies before claiming platform support. A macOS
CLI makes a skill macOS-specific; a cross-platform CLI requires verification on
its supported hosts. Document MCP/provider prerequisites and preserve SDK package
names when their upstream spelling is the real runtime contract.

Credentials belong in the secrets path, behavior in configuration. Helpers that
edit YAML must preserve Mercury's `models`, `approvals`, `hermes`, and `omp`
subtrees and operate on the selected profile. Avoid hardcoded stable homes in
nightly workflows. Do not restart live services while validating a skill unless
the user has explicitly requested that operation.

## Validation

Use the repository's skill manager validator and actual skill discovery. Names
and slash-command slugs must be unique across bundled and optional source skills.
All local references and scripts must resolve. If consolidating shipped skills,
ensure updates retire pristine obsolete copies while retaining custom user work.
