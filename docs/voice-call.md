# mLounge voice calls (Hermes engines only)

Browser calls use the Hermes STT/TTS providers selected in Mercury Setup.
The call screen uses Mercury's 🌡️ identity, a duration timer, and Audio,
End, and Mute controls. No browser provider or service credentials are needed.
Ordinary text chat remains independent of voice.

## Three tiers, no localhost

The caller is a **browser** on any machine. All three tiers may be
different boxes; nothing assumes shared audio devices or loopback.

```
browser (mic capture + speaker playback)
  │ getUserMedia mic up / HTMLAudio down — one /call socket per callId
  ▼
mLounge host — STT sidecar + signaling relay (observatory/voice_call_stt.py)
  │ transcripts up (returned to the browser; the browser delivers them
  │ over the existing chat socket) / TTS audio down (proxied from MIRC)
  ▼
MIRC host — Hermes loop + TTS + call registry
  │ unchanged gateway machinery (gateway sessions keyed by channel)
  │ TTS via the tts.provider from `mercury setup` (/api/audio/speak)
  ▼
Hermes engine (never OMP — refused at call start)
```

- Mic capture happens ONLY in the browser. The sidecar never opens a
  host microphone; every byte it transcribes arrived over the network.
- Playback happens ONLY in the browser. Hosts move bytes; no tier plays
  through a server-side speaker.
- The mLounge host is signaling + relay: browser<->mLounge on one
  session socket (per-call IDs), mLounge<->MIRC over HTTP.
- Barge-in, mute, and hangup are browser-side controls. Muting stops
  chunk upload (the registry is synced best-effort); barge-in pauses
  local playback when fresh mic energy arrives mid-reply; hangup closes
  the socket and ends the MIRC-side registry entry.

## Browser controls and lifecycle

The phone button starts the configured call through the existing authenticated
mLounge session. Missing host configuration, local dependencies/model assets,
service authentication, microphone permission, and engine failures are visible
host Setup/doctor remediation—not a request for browser credentials.

Each start carries a monotonically increasing socket-scoped `attemptId` through
controls and returned frames. It is correlation, not authority: the relay still
requires the owning authenticated session, exact origin/network and live target.
The relay mints a local `callId` and maps the sidecar's separate authoritative
upstream ID; neither browser field selects a MIRC registry ID. End cancels the
pending attempt before any ready ACK, and late frames cannot bind a newer call.
A cancelled status lookup never starts the registry. If start was already in
flight, its outcome remains uncertain until exact-ID end reconciliation; it is
not replayed and no bare-channel cleanup is used.

The duration is `mm:ss`, measured from the sidecar's `ready` establishment,
not from the button click or the microphone permission prompt. Established
calls show no waveform, transcript, or connection status. Transcripts still
travel through normal chat, and agent replies still trigger TTS.

The `ready.agentName` metadata comes from the live/durable Observatory node's
configured `name`, not its room ID, slug, or parent prefix. The browser preserves
that identity across navigation. Before metadata is available it uses a query's
actual nick, or a tagged assistant reply's `from.nick`, and otherwise shows the
intact room name rather than guessing an agent name from a channel slug.

Query calls signal the actual IRC nick; the backend resolves a live node's
`mxid` rather than guessing a channel from its slug. `ready.agentRoom` carries
the authoritative `room_id` for lifecycle tracking, while transcripts continue
to use the original query's numeric mLounge chat target. Removing that query,
its network, or its registered room ends the call. A self-PART removing the
agent room also ends a query call even if the query remains open; ordinary view
navigation and ready-metadata changes are not treated as removal.

- **Audio** directly switches the call's persistent `HTMLAudioElement` using
  `setSinkId` between exposed playback routes on each tap, then back on the next
  tap. The icon, colour, and actual device label update only after a completed
  switch. Default/communications aliases are not invented alternatives; a group
  identifies their current physical route only when that mapping is unambiguous.
  Distinct non-alias device IDs remain distinct logical routes even when their
  `groupId` matches. The picker remains available for explicit selection,
  additional outputs, and permission/fallback cases. A rejected switch preserves
  the prior route and shows the error. Unsupported browsers or devices exposing
  no alternate route visibly explain browser/system-default playback and device
  settings. An earpiece route is never claimed unless actually exposed. Each new
  call starts with the browser default and fresh output state, not a stale claim
  about the last call's device.
- **Mute** immediately disables microphone tracks, stops segment recording,
  and invalidates pending uploads. Unmute begins a fresh recording so bytes
  captured before mute cannot leak after unmute. The microphone slash denotes
  the currently **unmuted** state, as specified by this UI's control convention;
  the visible label stays **Mute**, while the pressed state and action's
  accessible name distinguish mute from unmute.
- **End** immediately stops recording and mic tracks, clears playback and the
  timer, sends `hangup` if possible, and closes the socket. The sidecar releases
  its registry entry. Late permission, recording, playback, or socket callbacks
  cannot reopen the screen.
- The top-right four-arrow control minimizes the call into PiP or expands it.
  Minimize does not stop audio. Channel, query, and settings navigation preserve
  the active call and its original contact, chat input target, and TTS source.
  Calling a different contact while a call exists restores the existing call;
  end it before calling another contact. Closing the app tears the call down.

Rejected browser playback retains the queued reply. Audio retries it immediately
within the click's user activation, before waiting for output enumeration.
Playback failures remain visible independently of output-selection messages.
A worker/provider error ends the established state and exposes its error;
repair the selected host's configuration through Mercury Setup/doctor.

Acceptance: a remote laptop browser against muted/headless servers
captures mic locally and plays replies through the laptop speakers —
no server audio device is ever addressed.

## Setup options (`mercury setup stt`, re-runnable)

Three starting STT options, persisted as `stt.provider` plus
`{model, language, endpoint}` per-provider blocks:

| Option | Runs | Key |
|---|---|---|
| `qwen3-asr` | local CLI (`qwen3-asr`, default model `Qwen3-ASR-1.7B`) | none |
| `parakeet` | local CLI (`parakeet-transcribe`, default `parakeet-tdt-0.6b-v2`) | none |
| `openai` | Whisper API (`whisper-1`) | `VOICE_TOOLS_OPENAI_KEY` / `OPENAI_API_KEY` in `.env`, never the repo |

The local two resolve through the existing `stt.providers.<name>`
command registry (`local: true` exempts them from the remote upload
cap) — no new native backends, no new dependencies. `endpoint` is
baked into the command template as `--endpoint` when set (edit the
template if your CLI uses a different flag).

Non-interactive: `mercury setup stt --non-interactive
--stt-provider parakeet [--stt-model … --stt-endpoint … --stt-language …]`,
or `MERCURY_STT_PROVIDER/MODEL/ENDPOINT/LANGUAGE` env vars. The OpenAI
key always comes from the environment/`.env`.

### Local model prerequisites: no automatic weight downloads

Provision local provider binaries and model assets separately, then configure
their existing paths. Model selectors shown in setup are not permission to fetch
weights. Mercury does not automatically download model weights or substitute a
cloud provider when local assets are absent.

The existing local faster-whisper path requires readable, nonempty `model.bin`,
`config.json`, and `tokenizer.json` in `stt.local.model` (a model directory) or a
complete existing cache. The Whisper CLI requires an existing local `.pt`
checkpoint, not a bare model name that would trigger a download. Local TTS also
requires complete assets: Piper's ONNX model plus JSON configuration, Kitten's
local model assets, or NeuTTS's backbone, codec, and semantic encoder assets.
Missing assets are an actionable host-configuration error; configure STT on the
mLounge host and TTS on the MIRC host before retrying. Explicitly configured
cloud APIs remain available with their own credentials.

## Setup-managed authenticated transport

The browser uses its existing private mLounge Socket.IO login. The public
`voiceCallSidecarUrl` key advertises `socket.io:/call` when the host's private
bootstrap is available. It never contains a provider key or service token.
The browser must not use legacy URL/token localStorage overrides.

Setup retains the existing explicit three-host `voice_call` configuration:

- `mirc_host_url`: the selected MIRC host's existing voice API
  (`/api/voice-call/status`, `/api/voice-call/call`, `/api/audio/speak`).
- `mlounge_host_url`: the exact browser origin allowed to initiate calls.
- `stt_sidecar_url`: the private STT service peer on the mLounge host;
  this URL is consumed server-side, not opened by the browser.
- `enabled`, and the canonical saved `stt` and `tts` sections.

`mercury setup stt` retains `--mirc-url`, `--mlounge-url`, and `--sidecar-url`.
All peers are explicit: a remote host is never replaced with localhost.
When all tiers run on one selected installation, provisioning creates both
the STT and authenticated MIRC voice API units from these configured endpoints.
An existing installation's normal Setup rerun regenerates the private bootstrap
and unit definitions; no separate manual sidecar command is the normal path.
The STT service reloads the selected provider/model/language at the next call.
Provider credentials resolve through the canonical selected-home `.env` helpers.

The installation home and named profile are pinned in generated unit
environment variables. Stable and nightly voice unit names are distinct.
The private bootstrap contains only configuration paths, explicit peers,
authorized owner/network context, and profile identity. Service secrets stay
in the selected host/profile `.env`; neither unit arguments, unit text,
bootstrap JSON, browser payloads, nor public YAML contain them.

On a split deployment, run Setup/provisioning on the host owning each service.
The mLounge host must have its explicit STT peer, canonical STT configuration,
authorized provisioned IRC network and owner login. The MIRC host must have
its canonical TTS configuration and selected profile. The corresponding
`VOICE_CALL_SIDECAR_TOKEN` and `VOICE_CALL_MIRC_TOKEN` service pairs must be
provisioned securely in the participating hosts' `.env` stores; Setup refuses
an unconfigured remote peer rather than generating an unrelated remote secret.
These are host-deployment prerequisites, never per-browser inputs.
Use HTTPS for split-host service traffic or an explicitly encrypted private
network. Direct HTTPS voice listeners require `voice_call.tls_cert` and
`voice_call.tls_key` pointing to existing host certificate/key files.

The private listener authenticates every effectful route using a service
header; missing authentication fails closed. Token URLs, wildcard CORS,
browser origins on the internal listener, arbitrary redirects and
client-selected peer/profile/provider URLs are rejected. mLounge resolves
the numeric target only inside the authenticated owner's connected,
provisioned IRC network and joined channel/query context. Each control and
audio frame is bound to that socket's returned call ID. Session revocation,
target removal and disconnect invalidate the relay; stale call IDs cannot
mutate a new call. OMP/unknown/foreign-profile targets are refused before
STT or TTS execution.

Remote browsers require an HTTPS mLounge origin for `getUserMedia`.
A plain HTTP tailnet hostname is not a secure microphone context.
The authenticated same-origin session relay avoids mixed-content browser
connections to an internal HTTP service; it does not excuse insecure
host-to-host deployment.


## Retained call protocol

The browser emits `voice:call` on its existing authenticated Socket.IO
connection. The first object is `{type: hello, target, mime}`, with a numeric
mLounge channel/query target. Subsequent audio is
`{type: audio, callId, data}` containing a complete independently decodable
browser recording. Controls retain `tts`, `mute`, `hangup`, and `ping`, and
include the returned `callId`. mLounge resolves the target server-side and
forwards the retained `/call` hello/binary/control wire to its private STT
peer using a server-only authentication header.
- Sidecar → browser: `{type: ready, callId, engine, sttProvider, agentName?, agentRoom?}`,
  `{type: refused, reason}` (OMP and unidentified/expired rooms),
  `{type: transcript, text}`, `{type: audio, token, mime, dataUrl}`,
  `{type: muted}`, `{type: ended}`, `{type: error}`.
- Transcripts return to the browser, which sends them as plain channel
  input — the normal MIRC inbound path into the Hermes loop. No new
  message kind on the MIRC side.
- Agent replies tagged `assistant_reply` are watched in-channel by the
  call panel; tool output, thinking, status and user messages are skipped.
  Each new reply is sent via the socket to the sidecar, which
  proxies MIRC `/api/audio/speak` and streams the data URL back down
  the same socket for browser playback.

## Hermes-only scope

`observatory/voice_call.py` resolves the channel engine through the
live room manager, or the read-only durable Observatory tree when the
web server runs in another process. Only a confirmed live Hermes room
can start a call. The 📞
button mounts on channel/query views; starting a call against an OMP
room surfaces the refusal in the panel. OMP engines are deferred —
no OMP code path was touched.

## Validation limits

Automated checks cover the actual HTTP/WebSocket upgrade, service auth, engine
selection, independently decodable microphone recordings, transcript/TTS delivery,
and blocked-worker hangup. Client controls additionally cover ready-based timing,
pending permission/upload cancellation, mute/unmute races, PiP/contact pinning,
direct Audio taps, alias/default-group boundaries, output switching/failure,
fresh-call routing state, autoplay retry, visible worker failure, and actual
query/registered-room/network removal.

An offline browser smoke mounts the actual App, Chat, and VoiceCall components
with unrelated leaf views stubbed. Generated browser media runs through native
MediaRecorder, the source sidecar and registry with fake STT/TTS providers, normal
chat input, and native HTMLAudio playback. Screenshots verify the full screen and
PiP; control checks verify muted upload suppression, socket/mic teardown, an empty
registry after End, missing configuration, and OMP refusal. Deterministic fake
output devices exercise successful sink selection and rejection; the native API's
rejection path is also observed. These checks do not establish physical device
routing.

The source query smoke verifies `Kai` → registered node identity/canonical room,
native microphone bytes → STT → numeric query chat target, and TTS → native
playback. Expiring the temporary node and delivering a self-PART through the
actual client handler ends the call while the query remains open. A fake STT
provider failure through the real worker also ends the call visibly.

Isolated Chromium playback smoke used `--autoplay-policy=no-user-gesture-required`
for deterministic native playback. A rejection injected at the play seam proves
the queued reply survives and Audio retries the same source through native
playback; unit checks also hold enumeration pending to verify the retry remains
inside the gesture. This does not prove default-policy or Safari/iOS autoplay
acceptance.

Physical microphones, acoustic echo/barge-in thresholds, uninterrupted speech
across recording boundaries, provider latency/audio quality, and Safari/iOS
hardware/playback permissions still need hardware acceptance. No live provider
quota or live Mercury installation is required for the offline smoke.

## Files

- `hermes/mercury_cli/setup.py` — `setup_stt` section + host URLs.
- `hermes/mercury_cli/subcommands/setup.py` — `stt` section + flags.
- `hermes/mercury_cli/config_defaults.py` — `stt` qwen/parakeet
  blocks, `voice_call` section.
- `hermes/tools/transcription_tools.py` — `local: true` honor for
  command STT providers.
- `hermes/observatory/voice_call.py` — engine guard, config, registry.
- `hermes/observatory/voice_call_stt.py` — sidecar (HTTP + WS).
- `hermes/mercury_cli/web_server.py`, `web_models.py` —
  `/api/voice-call/status`, `/api/voice-call/call` (audio reuses
  `/api/audio/speak`).
- `third_party/mlounge/client/components/VoiceCall.vue` (`App.vue` owner,
  `Chat.vue` phone button, `mercury.css`) — persistent call UI,
  browser recording/playback, barge-in, mute/hangup, PiP, and output routing.
- `hermes/.env.example` — STT/host/token placeholders.
