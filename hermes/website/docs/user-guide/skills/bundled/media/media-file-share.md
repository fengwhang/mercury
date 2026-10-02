---
title: "File Share — Send files in chat as links (paperclip, attachment)"
sidebar_label: "File Share"
description: "Send files in chat as links (paperclip, attachment)"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# File Share

Send files in chat as links (paperclip, attachment).

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/media/file-share` |
| Version | `1.0.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `IRC`, `Lounge`, `Files`, `Sharing` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Mercury loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# File Share (Lounge paperclip)

Post a local file in the room you are already in. Same result as the
human clicking the attachment button: a link only visible in that room.

## Procedure (one tool call — do NOT reimplement with terminal)

Call the tool for your engine. It stages, verifies, and posts by
itself. If it errors, report the error text; never hand-run the
staging steps below the hood.

Hermes: `lounge_share(path, caption?)`. OMP: `share_file(path,
caption?)`, then post the returned URL in your reply.

## Rules

- Links work only for whoever can reach this box's Lounge. Never paste
one into a different room.
- No size cap, but uploads never expire — don't share dumps casually.
- System paths, credentials, and anything under `~/.mercury` (except
prior uploads) are refused. If refused, say so and stop.
