<template>
	<div class="voice-call">
		<button
			class="call-toggle"
			:class="{active: inCall, connecting: connecting}"
			aria-label="Start or end voice call"
			:title="inCall ? 'End voice call' : 'Start experimental voice call'"
			@click="toggleCall"
		>
			📞
		</button>
		<div v-if="panelOpen" class="voice-call-panel">
			<div class="voice-call-status">
				<span class="voice-dot" :class="connectionClass" />
				<span class="voice-label">{{ statusLabel }}</span>
				<button
					class="voice-settings-toggle"
					aria-label="Voice call settings"
					@click="showSettings = !showSettings"
				>
					⚙
				</button>
			</div>
			<canvas ref="waveform" class="voice-waveform" width="260" height="48" />
			<div class="voice-level">
				<div class="voice-level-fill" :style="{width: levelPercent + '%'}" />
			</div>
			<div class="voice-controls">
				<button
					:disabled="!inCall"
					:class="{muted: muted}"
					aria-label="Mute or unmute"
					@click="toggleMute"
				>
					{{ muted ? "🔇" : "🎙" }}
				</button>
				<button :disabled="!inCall" aria-label="Hang up" @click="hangup">🛑</button>
			</div>
			<div v-if="showSettings" class="voice-settings">
				<label
					>mLounge-host sidecar URL (explicit, no localhost default)
					<input
						v-model="sidecarUrl"
						placeholder="http://mlounge-host:8765"
						spellcheck="false"
					/>
				</label>
				<label
					>Sidecar token (when set server-side)
					<input v-model="token" type="password" placeholder="optional" />
				</label>
				<button @click="saveSettings">Save</button>
			</div>
			<div v-if="error" class="voice-error">{{ error }}</div>
			<ul class="voice-transcripts">
				<li v-for="(line, i) in transcripts" :key="i" :class="line.kind">
					{{ line.text }}
				</li>
			</ul>
		</div>
	</div>
</template>

<script lang="ts">
import {defineComponent, PropType, ref, computed, watch, onBeforeUnmount, nextTick} from "vue";
import socket from "../js/socket";
import {recordVoiceSegments} from "../js/helpers/voice-recording";
import {useStore} from "../js/store";
import type {ClientNetwork, ClientChan} from "../js/types";

type TranscriptLine = {kind: "said" | "heard"; text: string};

const SIDECAR_URL_KEY = "mlounge.voiceCall.sidecarUrl";
const SIDECAR_TOKEN_KEY = "mlounge.voiceCall.token";
const CHUNK_MS = 2000;

function wsBase(url: string): string {
	const trimmed = url.trim().replace(/\/+$/, "");

	if (trimmed.startsWith("https://")) {
		return "wss://" + trimmed.slice("https://".length);
	}

	if (trimmed.startsWith("http://")) {
		return "ws://" + trimmed.slice("http://".length);
	}

	if (trimmed.startsWith("ws://") || trimmed.startsWith("wss://")) {
		return trimmed;
	}

	return "ws://" + trimmed;
}

function pickRecorderMime(): string {
	try {
		const MR = (window as any).MediaRecorder;

		if (!MR || typeof MR.isTypeSupported !== "function") {
			return "";
		}

		for (const mime of [
			"audio/webm;codecs=opus",
			"audio/webm",
			"audio/ogg;codecs=opus",
			"audio/mp4",
		]) {
			if (MR.isTypeSupported(mime)) {
				return mime;
			}
		}
	} catch {
		return "";
	}

	return "";
}

function stripIrcFormatting(text: string): string {
	return text
		.replace(/\x03\d{0,2}(,\d{0,2})?/g, "")
		.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
		.trim();
}

export default defineComponent({
	name: "VoiceCall",
	props: {
		network: {type: Object as PropType<ClientNetwork>, required: true},
		channel: {type: Object as PropType<ClientChan>, required: true},
	},
	setup(props) {
		const store = useStore();
		const inCall = ref(false);
		const connecting = ref(false);
		const muted = ref(false);
		const panelOpen = ref(false);
		const showSettings = ref(false);
		const error = ref("");
		const level = ref(0);
		const transcripts = ref<TranscriptLine[]>([]);
		const sidecarUrl = ref("");
		const token = ref("");
		const waveform = ref<HTMLCanvasElement | null>(null);
		const playing = ref(false);

		let ws: WebSocket | null = null;
		let stream: MediaStream | null = null;
		let audioCtx: AudioContext | null = null;
		let analyser: AnalyserNode | null = null;
		let recorder: {stop: () => void} | null = null;
		let callEpoch = 0;
		let rafId = 0;
		let noiseFloor = 0.02;
		let playStartedAt = 0;
		let audioEl: HTMLAudioElement | null = null;
		let audioQueue: Array<{token: string; dataUrl: string}> = [];
		let lastSeenId = 0;
		let ttsSeq = 0;
		let discardTtsThrough = 0;
		let lastSpeechAt = 0;
		let callActive = false;

		try {
			sidecarUrl.value = window.localStorage.getItem(SIDECAR_URL_KEY) || "";
			token.value = window.localStorage.getItem(SIDECAR_TOKEN_KEY) || "";
		} catch {
			sidecarUrl.value = "";
		}

		const levelPercent = computed(() => Math.round(Math.min(1, level.value) * 100));
		const connectionClass = computed(() => {
			if (error.value) {
				return "error";
			}

			if (inCall.value) {
				return "live";
			}

			if (connecting.value) {
				return "connecting";
			}

			return "idle";
		});
		const statusLabel = computed(() => {
			if (error.value) {
				return "Call failed";
			}

			if (inCall.value) {
				return muted.value ? "On call — muted" : "On call";
			}

			if (connecting.value) {
				return "Connecting…";
			}

			return "Tap 📞 to call this agent";
		});

		const saveSettings = () => {
			try {
				window.localStorage.setItem(SIDECAR_URL_KEY, sidecarUrl.value.trim());
				window.localStorage.setItem(SIDECAR_TOKEN_KEY, token.value);
			} catch {
				// private mode — settings last for the session only
			}

			showSettings.value = false;
		};

		const stopTracks = () => {
			if (rafId) {
				cancelAnimationFrame(rafId);
				rafId = 0;
			}

			try {
				recorder?.stop();
			} catch {
				// already stopped
			}

			recorder = null;

			try {
				stream?.getTracks().forEach((track) => track.stop());
			} catch {
				// already stopped
			}

			stream = null;

			if (audioCtx) {
				void audioCtx.close().catch(() => undefined);
				audioCtx = null;
			}

			analyser = null;

			if (audioEl) {
				try {
					audioEl.pause();
				} catch {
					// already paused
				}

				audioEl = null;
			}

			audioQueue = [];
			playing.value = false;
			level.value = 0;
		};

		const teardown = (message: string) => {
			callEpoch += 1;
			callActive = false;
			inCall.value = false;
			connecting.value = false;
			muted.value = false;

			if (message) {
				error.value = message;
			}

			try {
				ws?.close();
			} catch {
				// already closed
			}

			ws = null;
			stopTracks();
		};

		const playNext = () => {
			if (playing.value || audioQueue.length === 0 || !callActive) {
				return;
			}

			const next = audioQueue.shift();

			if (!next) {
				return;
			}

			try {
				audioEl = new Audio(next.dataUrl);
				playing.value = true;
				playStartedAt = performance.now();

				audioEl.onended = () => {
					playing.value = false;
					audioEl = null;
					playNext();
				};

				audioEl.onerror = () => {
					playing.value = false;
					audioEl = null;
					playNext();
				};

				void audioEl.play().catch(() => {
					playing.value = false;
					audioEl = null;
				});
			} catch {
				playing.value = false;
				audioEl = null;
			}
		};

		const meterLoop = () => {
			if (!analyser || !callActive) {
				return;
			}

			const data = new Uint8Array(analyser.fftSize);
			analyser.getByteTimeDomainData(data);
			let sum = 0;

			for (let i = 0; i < data.length; i++) {
				const v = (data[i] - 128) / 128;
				sum += v * v;
			}

			const rms = Math.sqrt(sum / data.length);
			level.value = rms;

			if (!muted.value && rms > Math.max(noiseFloor * 2, 0.02)) {
				lastSpeechAt = performance.now();
			}

			// Barge-in (browser-side): loud mic input while a reply plays
			// stops local playback. The agent turn itself is untouched —
			// the next transcript steers it.
			if (!muted.value && playing.value && performance.now() - playStartedAt > 500) {
				const threshold = Math.max(noiseFloor * 3, 0.08);

				if (rms > threshold && audioEl) {
					try {
						audioEl.pause();
					} catch {
						// already paused
					}

					audioEl = null;
					playing.value = false;
					audioQueue = [];
					discardTtsThrough = ttsSeq;
				}
			}

			const canvas = waveform.value;

			if (canvas) {
				const ctx = canvas.getContext("2d");

				if (ctx) {
					ctx.clearRect(0, 0, canvas.width, canvas.height);
					ctx.beginPath();
					const step = canvas.width / data.length;

					for (let i = 0; i < data.length; i++) {
						const y =
							((data[i] - 128) / 128) * canvas.height * 0.45 + canvas.height / 2;

						if (i === 0) {
							ctx.moveTo(0, y);
						} else {
							ctx.lineTo(i * step, y);
						}
					}

					ctx.stroke();
				}
			}

			rafId = requestAnimationFrame(meterLoop);
		};

		const startMeter = (media: MediaStream) => {
			const AC = (window as any).AudioContext || (window as any).webkitAudioContext;
			audioCtx = new AC() as AudioContext;
			const source = audioCtx.createMediaStreamSource(media);
			analyser = audioCtx.createAnalyser();
			analyser.fftSize = 2048;
			source.connect(analyser);
			// Calibrate the quiet-room floor before the user speaks.
			const probe = new Uint8Array(analyser.fftSize);
			let samples = 0;
			let acc = 0;

			const calibrate = () => {
				if (!analyser || samples >= 10) {
					noiseFloor = Math.max(acc / Math.max(1, samples), 0.005);
					rafId = requestAnimationFrame(meterLoop);
					return;
				}

				analyser.getByteTimeDomainData(probe);
				let sum = 0;

				for (let i = 0; i < probe.length; i++) {
					const v = (probe[i] - 128) / 128;
					sum += v * v;
				}

				acc += Math.sqrt(sum / probe.length);
				samples += 1;
				setTimeout(calibrate, 50);
			};

			calibrate();
		};

		const startRecorder = (media: MediaStream, mime: string) => {
			recorder = recordVoiceSegments(
				media,
				mime,
				(blob) => {
					if (!callActive || muted.value || !ws || ws.readyState !== WebSocket.OPEN) {
						return;
					}

					const callSocket = ws;
					void blob.arrayBuffer().then((buffer) => {
						if (
							ws === callSocket &&
							callSocket.readyState === WebSocket.OPEN &&
							callActive &&
							!muted.value
						) {
							callSocket.send(buffer);
						}
					});
				},
				CHUNK_MS,
				{
					canFinalize: () => performance.now() - lastSpeechAt >= 350,
					onError: (recordingError) => teardown(recordingError.message),
				}
			);
		};

		const requestTts = (text: string) => {
			if (!ws || ws.readyState !== WebSocket.OPEN || !callActive) {
				return;
			}

			ttsSeq += 1;
			ws.send(JSON.stringify({type: "tts", text, token: `reply-${ttsSeq}`}));
		};

		const onSocketMessage = (event: MessageEvent) => {
			let message: any;

			try {
				message = JSON.parse(String(event.data));
			} catch {
				return;
			}

			if (!message || typeof message !== "object") {
				return;
			}

			switch (message.type) {
				case "ready":
					connecting.value = false;
					inCall.value = true;
					callActive = true;
					error.value = "";
					lastSeenId = 0;

					for (const m of props.channel.messages) {
						if (typeof m.id === "number" && m.id > lastSeenId) {
							lastSeenId = m.id;
						}
					}

					transcripts.value.push({
						kind: "heard",
						text: `Connected (STT: ${message.sttProvider || "sidecar"})`,
					});
					break;
				case "refused":
					teardown(String(message.reason || "Call refused"));
					break;

				case "transcript": {
					const text = String(message.text || "").trim();

					if (!text || !callActive) {
						break;
					}

					transcripts.value.push({kind: "said", text});

					if (store.state.isConnected) {
						socket.emit("input", {target: props.channel.id, text});
					}

					break;
				}

				case "audio":
					if (
						message.dataUrl &&
						callActive &&
						Number(String(message.token || "").replace(/^reply-/, "")) >
							discardTtsThrough
					) {
						audioQueue.push({
							token: String(message.token || ""),
							dataUrl: String(message.dataUrl),
						});
						playNext();
					}

					break;
				case "muted":
					muted.value = Boolean(message.muted);
					break;
				case "ended":
					teardown("");
					break;
				case "error":
					error.value = String(message.message || "Call error");
					break;
				default:
					break;
			}
		};

		const hangup = () => {
			if (!inCall.value && !connecting.value) {
				panelOpen.value = false;
				return;
			}

			try {
				ws?.send(JSON.stringify({type: "hangup"}));
			} catch {
				// closing below ends the call server-side too
			}

			teardown("");
			panelOpen.value = false;
		};

		const toggleCall = async () => {
			if (inCall.value || connecting.value) {
				hangup();
				return;
			}

			error.value = "";
			const base = sidecarUrl.value.trim();

			if (!base) {
				error.value =
					"Set the sidecar URL first (⚙) — e.g. http://mlounge-host:8765. No localhost assumed.";
				panelOpen.value = true;
				showSettings.value = true;
				return;
			}

			panelOpen.value = true;
			connecting.value = true;
			const epoch = ++callEpoch;

			try {
				const acquired = await navigator.mediaDevices.getUserMedia({audio: true});

				if (epoch !== callEpoch) {
					acquired.getTracks().forEach((track) => track.stop());
					return;
				}

				stream = acquired;
			} catch {
				if (epoch !== callEpoch) {
					return;
				}

				teardown("Microphone blocked — allow mic access for this origin, then retry.");
				return;
			}

			try {
				startMeter(stream);
			} catch {
				teardown("WebAudio unavailable in this browser.");
				return;
			}

			let url = wsBase(base) + "/call";

			if (token.value) {
				url += "?token=" + encodeURIComponent(token.value);
			}

			try {
				ws = new WebSocket(url);
			} catch {
				teardown("Could not open the call socket — check the sidecar URL.");
				return;
			}

			const callSocket = ws;
			const mime = pickRecorderMime() || "audio/webm";

			ws.onopen = () => {
				if (ws !== callSocket) {
					return;
				}

				ws?.send(JSON.stringify({type: "hello", channel: props.channel.name, mime}));

				try {
					startRecorder(stream as MediaStream, mime);
				} catch (err) {
					teardown(err instanceof Error ? err.message : "Recorder failed to start");
				}
			};

			ws.onmessage = (event) => {
				if (ws === callSocket) {
					onSocketMessage(event);
				}
			};

			ws.onerror = () => {
				if (ws !== callSocket) {
					return;
				}

				if (!inCall.value) {
					teardown("Call socket error — is the sidecar reachable at that URL?");
				}
			};

			ws.onclose = () => {
				if (ws !== callSocket) {
					return;
				}

				if (callActive || connecting.value) {
					teardown("Call socket closed.");
				}
			};
		};

		const toggleMute = () => {
			if (!inCall.value) {
				return;
			}

			muted.value = !muted.value;

			try {
				ws?.send(JSON.stringify({type: "mute", muted: muted.value}));
			} catch {
				// registry sync is best-effort; the mic gate above is the real mute
			}
		};

		// Agent replies arriving in this channel while on a call are
		// synthesized on the MIRC host and played back here (browser
		// speakers — never a server-side device).
		watch(
			() => props.channel.messages.at(-1)?.id,
			() => {
				let maxId = lastSeenId;
				const replies: string[] = [];

				for (const m of props.channel.messages) {
					if (typeof m.id === "number" && m.id > maxId) {
						maxId = m.id;
					}

					if (
						callActive &&
						typeof m.id === "number" &&
						m.id > lastSeenId &&
						!m.self &&
						m.mercuryKind === "assistant_reply" &&
						typeof m.text === "string" &&
						m.text.trim() &&
						(m.type === undefined || String(m.type) === "message")
					) {
						const clean = stripIrcFormatting(m.text);

						if (clean) {
							replies.push(clean);
						}
					}
				}

				lastSeenId = maxId;

				for (const reply of replies) {
					transcripts.value.push({kind: "heard", text: reply});
					requestTts(reply);
				}
			}
		);

		onBeforeUnmount(() => {
			callEpoch += 1;

			try {
				ws?.send(JSON.stringify({type: "hangup"}));
			} catch {
				// unmounting — best effort
			}

			callActive = false;

			try {
				ws?.close();
			} catch {
				// already closed
			}

			ws = null;
			stopTracks();
		});

		return {
			store,
			inCall,
			connecting,
			muted,
			panelOpen,
			showSettings,
			error,
			levelPercent,
			transcripts,
			sidecarUrl,
			token,
			waveform,
			connectionClass,
			statusLabel,
			saveSettings,
			toggleCall,
			toggleMute,
			hangup,
		};
	},
});
</script>
