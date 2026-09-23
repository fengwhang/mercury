---
name: file-share
description: "Send files in chat as links (paperclip, attachment)."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  mercury:
    tags: [IRC, Lounge, Files, Sharing]
---

# File Share (Lounge paperclip)

Post a local file in the room you are already in. Same result as the
human clicking the attachment button: a link only visible in that room.

## Procedure (one tool call)

Hermes: `lounge_share(path, caption?)` stages, verifies, and posts the
link in your current room by itself — one call, done. OMP:
`share_file(path, caption?)` returns the URL; post it in your reply.

Do NOT reimplement with terminal commands. If the tool errors, report
the error text instead of working around it.

## Rules

- Links work only for whoever can reach this box's Lounge. Never paste
one into a different room.
- No size cap, but uploads never expire — don't share dumps casually.
- System paths, credentials, and anything under `~/.mercury` (except
prior uploads) are refused. If refused, say so and stop.
