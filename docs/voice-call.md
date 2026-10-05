# mLounge voice calls (Hermes engines only)

Experimental voice-call UX for Hermes-agent rooms: tap-to-call, live
waveform, barge-in, mute/hangup — inspired by the
OpenAI Dots Voice interaction model, implemented from scratch against
Mercury's own stack (no proprietary code).

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

## Split-host config (`voice_call` section)

All URLs explicit, empty by default — the call UI refuses to start
until they are set. Same-machine works by pointing them at the one
box; localhost is never filled in for you.

- `voice_call.mirc_host_url` — MIRC/gateway host
  (`/api/voice-call/*`, `/api/audio/speak`). Consumed by the sidecar.
- `voice_call.mlounge_host_url` — host serving the mLounge UI.
- `voice_call.stt_sidecar_url` — sidecar base the browser uses
  (e.g. `https://voice.example.ts.net`); stored per-browser in the call
  settings popover when it differs.
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
- Sidecar → browser: `{type: ready, callId, engine, sttProvider}`,
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

Automated tests cover the actual HTTP/WebSocket upgrade, service auth,
engine selection across processes, audio-container signaling, transcript
and TTS delivery, and hangup while transcription is blocked. Provider
audio quality, real browser microphone/playback, and uninterrupted
speech across recording boundaries still need an end-to-end hardware test.
Calls remain experimental and require explicit sidecar setup; ordinary
Observatory chat does not depend on voice services. Automated checks do not
establish production audio quality or Safari/iOS hardware acceptance.

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
- `third_party/mlounge/client/components/VoiceCall.vue` (+ `Chat.vue`
  mount, `mercury.css`) — 📞 button, panel, meter, barge-in/mute/hangup.
- `hermes/.env.example` — STT/host/token placeholders.
