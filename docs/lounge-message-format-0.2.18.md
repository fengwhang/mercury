# Lounge message formatting (0.2.18)

The Lounge fork previously guessed whether a message was a trace from its
leading emoji. It also parsed math before protecting inline code, so a command
such as `echo "$A-$B" *.txt` could turn into LaTeX. IRC splitting removed spaces
and added line breaks inside long commands, and observatory frame formatting
collapsed multiline replies and truncated their code fences.

Messages now carry a validated `+mercury/kind` IRC tag. Tool inputs, tool
outputs, thinking and status messages render literally. Assistant replies
retain Markdown and LaTeX, with inline and fenced code protected before math,
emphasis, links, emoji and IRC styling. The kind survives batch reassembly and
SQLite history reload. Old history without tags retains the emoji fallback.
The agent's IRC formatting hint asks it to fence commands and literal snippets.

UTF-8 chunks use continuation tags, and empty lines use an explicit sentinel,
so batch reassembly restores the exact message text. Assistant reply frames
retain their complete source. Trace size limits remain bounded as before.
Code blocks offer **Copy code**; messages offer **View raw** and **Copy raw**.
Copy uses source text, including whitespace, with a fallback for HTTP pages
where the browser Clipboard API is unavailable or denied.

## Validation

- 65 Web UI tests passed across ten files: parsing, mounted Vue copy/raw
  controls, actual IRC framework parsing, multiline inputs and SQLite history.
- 165 Python tests passed across twelve observatory and IRC adapter files,
  including real TCP sockets for the gateway and per-agent identities. The
  socket tests verify UTF-8 wire limits, literal whitespace, blank lines,
  continuation joining and message-kind isolation after a batch.
- The Lounge production client and server builds passed. ESLint and Prettier
  passed for the changed TypeScript and Vue files. Client TypeScript checking
  passed when explicitly including the existing Node types.

Four related Python tests fail on the untouched `39b8813b` baseline as well
and were excluded from the passing run:

- `test_tls_listener_serves_strict_clients`
- `test_new_channel_auto_joins_server_clients`
- `test_watchdog_closes_silent_connection`
- `test_ensure_multiline_requests_late_cap`

The unmodified client TypeScript configuration also reports the pre-existing
missing `NodeJS` namespace in `client/js/upload.ts`. Stylelint reports the same
four duplicate selectors in the existing compact chat layout on both trees.
These failures are not passing validation for this release.

Both Linux binaries are rebuilt with version 0.2.18 and the release includes
the prebuilt Lounge fork. The compiled x64 smoke test passes. ARM64 execution
and a live browser session remain for user testing; no installed service or
live account settings were changed during validation.
