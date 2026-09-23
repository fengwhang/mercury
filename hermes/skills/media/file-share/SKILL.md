---
name: file-share
description: "Share local files in the current IRC channel as Lounge links."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [IRC, Lounge, Files, Sharing]
---

# File Share (Lounge paperclip)

Post a local file in the room you are already in. Same result as the
human clicking the attachment button: a link only visible in that room.

## Tools (use the one for your engine)

- Hermes: `lounge_share(path, caption?)` — posts the link itself.
- OMP: `share_file(path, caption?)` — returns the URL; post it in reply.

## Rules

- Links work only for whoever can reach this box's Lounge. Never paste
one into a different room.
- No size cap, but uploads never expire — don't share dumps casually.
- System paths, credentials, and anything under `~/.mercury` (except
prior uploads) are refused. If refused, say so and stop.
