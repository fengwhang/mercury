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
