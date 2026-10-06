# mLounge voice calls (Hermes engines only)

Experimental browser calls for Hermes-agent rooms. The phone action opens
an established-call screen with the registered agent/contact name, a pink avatar,
rose-to-burgundy background, a duration timer, and Audio, End, and Mute controls.
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

The phone button starts a configured call directly. Missing or invalid sidecar
configuration opens visible settings with **Save and call**; microphone, network,
authentication, and engine failures remain visible instead of disappearing below
the chat header. The call surface is mounted outside that clipping header.

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
Playback failures remain visible independently of output-selection messages;
they are not silently replaced by routing status. A worker/provider error ends
the established state and exposes its error plus **Voice settings**, rather
than leaving an inaudible call hidden behind the timer.

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

## Split-host config (`voice_call` section)

All URLs explicit, empty by default — the call UI refuses to start
until they are set. Same-machine works by pointing them at the one
box; localhost is never filled in for you.

- `voice_call.mirc_host_url` — MIRC/gateway host
  (`/api/voice-call/*`, `/api/audio/speak`). Consumed by the sidecar.
- `voice_call.mlounge_host_url` — host serving the mLounge UI.
- `voice_call.stt_sidecar_url` — sidecar base the browser uses
  (e.g. `https://voice.example.ts.net`). Observatory service provisioning exports
  this nonsecret URL as `MERCURY_VOICE_CALL_SIDECAR_URL`; mLounge sends it in its
  public browser configuration. Reprovision/regenerate and restart the mLounge
  service after changing it; this is not a hot-reloaded setting. A per-browser
  URL override in voice settings takes precedence.
- `voice_call.language`, `voice_call.enabled`.

`mercury setup stt` prompts for all three (flags `--mirc-url`,
`--mlounge-url`, `--sidecar-url`, or `MERCURY_MIRC_URL` /
`MERCURY_MLOUNGE_URL` / `MERCURY_STT_SIDECAR_URL`).

## Running the sidecar (mLounge host)

After `mercury setup stt` on that host:

```
python -m observatory.voice_call_stt --host 0.0.0.0 --port 8765 \
    --mirc-url http://mirc-host:8000 --token s3cret
```

Stdlib only. `--provider/--model/--language/--endpoint` overlay the
stored STT config in memory. `--token` (or `VOICE_CALL_SIDECAR_TOKEN`)
gates every route except `/stt/health`; without it the port transcribes
for anyone who can reach it — bind loopback or firewall accordingly.

Set `VOICE_CALL_MIRC_TOKEN` to the same secret in the **MIRC web-server
process** and the **sidecar process** (or pass `--mirc-token` to the
sidecar). This authenticates the sidecar only to the dashboard's three
voice/audio routes. It does not grant access to configuration or other
dashboard endpoints. The browser receives only the distinct sidecar token.
Restart the two processes after adding the service secret.

The separate sidecar token is entered in the browser's voice settings and kept
in local browser storage. Service provisioning and public mLounge configuration
never export the MIRC authentication token or automatically publish a sidecar
secret. When a configured sidecar refuses authentication, open **Voice settings**
and enter its dedicated browser token.

For a remote browser, serve mLounge over HTTPS and put the sidecar behind
an HTTPS reverse proxy that supports WebSocket upgrades; use its HTTPS
URL in the panel. The sidecar itself serves HTTP. A plain tailnet HTTP
hostname is not a browser secure context for microphone access, and an
HTTPS page cannot use an insecure `ws://` sidecar. Tailscale Serve or an
equivalent proxy can provide the HTTPS endpoints.

## Call protocol (sidecar `/call` socket)

- Browser → sidecar: `{type: hello, channel, mime}` first; then complete
  independently recorded audio files (at least 2s, waiting for a speech
  pause up to a 12s cap; WebM/Ogg/MP4 depending on
  browser support), `{type: tts, text, token}`,
  `{type: mute, muted}`, `{type: hangup}`, `{type: ping}`.
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
