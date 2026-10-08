<template>
	<div class="voice-call">
		<button
			v-if="showEntry"
			class="call-toggle"
			aria-label="Start voice call"
			@click="startCall(channel)"
		>
			<svg viewBox="0 0 24 24" aria-hidden="true">
				<path
					d="M5 3h4l2 5-3 2c2 3 3 4 6 6l2-3 5 2v4c0 2-2 3-4 2C9 19 5 15 3 7c-1-2 0-4 2-4Z"
				/>
			</svg>
		</button>
		<Teleport to="body">
			<section
				v-if="panelOpen"
				class="voice-call-screen"
				:class="{minimized}"
				role="dialog"
				:aria-label="'Mercury voice call with ' + contactName"
				:aria-modal="!minimized"
			>
				<button
					v-if="inCall || connecting"
					class="voice-pip"
					:aria-label="minimized ? 'Expand call' : 'Minimize call'"
					@click="minimized = !minimized"
				>
					<svg
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						aria-hidden="true"
					>
						<path
							d="m9 9-6-6m0 5V3h5m7 6 6-6m-5 0h5v5M9 15l-6 6m0-5v5h5m7-6 6 6m-5 0h5v-5"
						/>
					</svg>
				</button>
				<div class="voice-identity">
					<div class="voice-avatar" aria-hidden="true">🌡️</div>
					<h2>{{ contactName }}</h2>
				</div>
				<div v-if="inCall" class="voice-timer" aria-label="Call duration">
					{{ duration }}
				</div>
				<div v-else class="voice-setup">
					<p v-if="connecting">Connecting…</p>
					<p v-if="error" class="voice-error" role="alert">{{ error }}</p>
					<button v-if="!connecting" @click="retryCall">Retry call</button>
				</div>
				<div v-if="showOutputs && inCall" class="voice-outputs">
					<label v-if="sinkSupported && outputs.length"
						>Playback output<select
							:value="selectedOutput"
							:disabled="outputBusy"
							@change="selectOutput"
						>
							<option value="">Browser default</option>
							<option
								v-for="output in outputs"
								:key="output.deviceId"
								:value="output.deviceId"
							>
								{{
									output.label || "Audio output " + (outputs.indexOf(output) + 1)
								}}
							</option>
						</select></label
					>
					<p role="status">{{ outputNotice }}</p>
					<p v-if="playbackError" role="alert">{{ playbackError }}</p>
				</div>
				<div class="voice-controls">
					<div v-if="inCall" class="voice-control">
						<button
							class="voice-audio"
							:class="{routed: !!selectedOutput, alternate: alternateOutput}"
							:data-output="selectedOutput"
							:title="'Playback: ' + routeLabel"
							:disabled="outputBusy"
							aria-label="Audio output"
							:aria-expanded="showOutputs"
							@click="toggleOutputs"
						>
							<svg viewBox="0 0 24 24" aria-hidden="true">
								<path
									v-if="selectedOutput"
									class="voice-route-icon"
									d="M15 3h6m-3-3 3 3-3 3"
									fill="none"
									stroke="currentColor"
									stroke-width="1.5"
								/>
								<path d="M3 9h4l5-5v16l-5-5H3Z" />
								<path
									d="M16 8q5 4 0 8m3-11q8 7 0 14"
									fill="none"
									stroke="currentColor"
									stroke-width="2"
								/>
							</svg></button
						><span>Audio</span>
					</div>
					<div class="voice-control">
						<button class="voice-end" aria-label="End call" @click="hangup">
							<svg viewBox="0 0 24 24" aria-hidden="true">
								<path d="M2 13q10-10 20 0v5h-6v-5q-4-2-8 0v5H2Z" />
							</svg></button
						><span>End</span>
					</div>
					<div v-if="inCall" class="voice-control">
						<button
							class="voice-mute"
							:class="{muted}"
							:aria-label="muted ? 'Unmute microphone' : 'Mute microphone'"
							:aria-pressed="muted"
							@click="toggleMute"
						>
							<svg viewBox="0 0 24 24" aria-hidden="true">
								<rect x="9" y="2" width="6" height="12" rx="3" />
								<path
									d="M5 10v2a7 7 0 0 0 14 0v-2m-7 9v3m-4 0h8"
									fill="none"
									stroke="currentColor"
									stroke-width="2"
								/>
								<path
									v-if="!muted"
									class="voice-mic-slash"
									d="m3 3 18 18"
									fill="none"
									stroke="currentColor"
									stroke-width="2"
								/>
							</svg></button
						><span>Mute</span>
					</div>
				</div>
			</section>
		</Teleport>
	</div>
</template>

<script lang="ts">
import {defineComponent, PropType, ref, shallowRef, computed, watch, onBeforeUnmount} from "vue";
import socket from "../js/socket";
import eventbus from "../js/eventbus";
import {recordVoiceSegments} from "../js/helpers/voice-recording";
import {useStore} from "../js/store";
import type {ClientChan} from "../js/types";
import {ChanType} from "../../shared/types/chan";

type PlaybackAudio = HTMLAudioElement & {setSinkId?: (id: string) => Promise<void>};
type VoiceFrame = Record<string, unknown>;
type VoiceSocket = {
	emit(event: "voice:call", frame: VoiceFrame): void;
	on(event: string, listener: (frame: unknown) => void): void;
	off(event: string, listener: (frame: unknown) => void): void;
};
type CallConnection = {send(frame: VoiceFrame | ArrayBuffer): void; close(): void};

// Monotonic for this authenticated browser socket, including component remounts.
let nextAttempt = 0;

export default defineComponent({
	name: "VoiceCall",
	props: {
		channel: {type: Object as PropType<ClientChan>, default: undefined},
		showEntry: {type: Boolean, default: true},
	},
	setup(props) {
		const store = useStore();
		const target = shallowRef<ClientChan>();
		const agentName = ref("");
		const agentRoom = ref("");
		const inCall = ref(false);
		const connecting = ref(false);
		const panelOpen = ref(false);
		const minimized = ref(false);
		const muted = ref(false);
		const error = ref("");
		const relay = socket as unknown as VoiceSocket;
		const elapsed = ref(0);
		const showOutputs = ref(false);
		const sinkSupported = ref(false);
		const outputs = ref<MediaDeviceInfo[]>([]);
		const selectedOutput = ref("");
		const alternateOutput = ref(false);
		const outputBusy = ref(false);
		const outputNotice = ref("");
		const playbackError = ref("");
		const routeLabel = ref("Browser default");
		let previousOutput = "";
		const playbackOutputs = computed(() => {
			// groupId identifies hardware, not necessarily a distinct logical playback route.
			const routes = outputs.value.filter(
				(device) => !["default", "communications"].includes(device.deviceId)
			);

			for (const aliasId of ["default", "communications"]) {
				const alias = outputs.value.find((device) => device.deviceId === aliasId);

				if (!alias?.groupId || routes.some((device) => device.groupId === alias.groupId)) {
					continue;
				}

				if (aliasId === "default") {
					routes.unshift(alias);
				} else {
					routes.push(alias);
				}
			}

			return routes;
		});

		const resolvePlaybackRoute = (id: string) => {
			const requested = outputs.value.find((device) => device.deviceId === (id || "default"));

			if (!requested || !["default", "communications"].includes(requested.deviceId)) {
				return id;
			}

			if (!requested.groupId) {
				return null;
			}

			let match: MediaDeviceInfo | undefined;

			for (const route of playbackOutputs.value) {
				if (route.groupId !== requested.groupId) {
					continue;
				}

				if (match) {
					return null;
				} // Multiple real routes in this hardware group: do not guess.

				match = route;
			}

			return match ? (match.deviceId === "default" ? "" : match.deviceId) : null;
		};

		const contactName = computed(() => {
			if (agentName.value) {
				return agentName.value;
			}

			const channel = target.value || props.channel;

			if (!channel) {
				return "Voice call";
			}

			if (channel.type === ChanType.QUERY) {
				return channel.name;
			}

			for (let index = channel.messages.length - 1; index >= 0; index--) {
				const message = channel.messages[index];

				if (message.mercuryKind === "assistant_reply" && message.from?.nick) {
					return message.from.nick;
				}
			}

			// Without registered identity, show the actual room, never guess from its slug.
			return channel.name;
		});
		const duration = computed(
			() =>
				`${String(Math.floor(elapsed.value / 60)).padStart(2, "0")}:${String(
					elapsed.value % 60
				).padStart(2, "0")}`
		);
		let ws: CallConnection | null = null;
		let stream: MediaStream | null = null;
		let recorder: {stop: () => void} | null = null;
		let epoch = 0;
		let uploadEpoch = 0;
		let establishedAt = 0;
		let timer: number | undefined;
		let audio: PlaybackAudio | null = null;
		let playing = false;
		let queue: string[] = [];
		let ttsSeq = 0;
		let lastSeenId = 0;
		let audioCtx: AudioContext | null = null;
		let frame = 0;
		let lastSpeechAt = 0;
		let playStartedAt = 0;
		let discardThrough = 0;

		try {
			// Remove obsolete browser-side service credentials without ever reading them.
			localStorage.removeItem("mlounge.voiceCall.sidecarUrl");
			localStorage.removeItem("mlounge.voiceCall.token");
		} catch {
			// Storage may be disabled; voice does not depend on it.
		}

		const stopRecording = () => {
			uploadEpoch += 1;

			try {
				recorder?.stop();
			} catch {
				/* Release tracks even when the recorder already failed. */
			}

			recorder = null;
		};

		const teardown = (message = "") => {
			epoch += 1;
			inCall.value = false;
			connecting.value = false;
			const closing = ws;

			if (closing) {
				try {
					closing.send({type: "hangup"});
				} catch {
					// Closing still releases the server-side registry when hangup cannot be sent.
				}

				ws = null;

				try {
					closing.close();
				} catch {
					// A disconnected socket is already being cleaned up remotely.
				}
			}

			stopRecording();
			stream?.getTracks().forEach((track) => track.stop());
			stream = null;
			cancelAnimationFrame(frame);
			frame = 0;
			void audioCtx?.close().catch(() => undefined);
			audioCtx = null;

			if (audio) {
				audio.onended = audio.onerror = null;
				audio.pause();
				audio.removeAttribute("src");
				audio = null;
			}

			queue = [];
			playing = false;
			clearInterval(timer);
			timer = undefined;
			elapsed.value = 0;
			muted.value = false;
			showOutputs.value = false;
			playbackError.value = "";
			outputBusy.value = false;
			alternateOutput.value = false;
			selectedOutput.value = "";
			previousOutput = "";
			routeLabel.value = "Browser default";
			outputs.value = [];
			outputNotice.value = "";
			error.value = message;

			if (!message) {
				panelOpen.value = false;
			}
		};

		const hangup = () => teardown();

		const playNext = () => {
			if (playing || !inCall.value || !audio || !queue.length) {
				return;
			}

			const current = audio;
			const callEpoch = epoch;
			current.src = queue.shift()!;
			playbackError.value = "";
			playing = true;
			playStartedAt = performance.now();

			const finished = () => {
				if (epoch !== callEpoch || audio !== current) {
					return;
				}

				playing = false;
				playNext();
			};

			current.onended = finished;

			current.onerror = () => {
				if (epoch !== callEpoch) {
					return;
				}

				playbackError.value =
					"Reply audio could not be played. Check your browser playback permissions.";
				showOutputs.value = true;
				finished();
			};

			void current.play().catch(() => {
				if (epoch !== callEpoch) {
					return;
				}

				playing = false;
				playbackError.value =
					"Playback was blocked. Allow audio in your browser, then press Audio to retry.";
				showOutputs.value = true;
				queue.unshift(current.src);
			});
		};

		const refreshOutputs = async () => {
			const callEpoch = epoch;

			try {
				const devices = await navigator.mediaDevices.enumerateDevices();

				if (callEpoch !== epoch) {
					return;
				}

				outputs.value = devices.filter(
					(device) => device.kind === "audiooutput" && device.deviceId
				);
				outputNotice.value =
					sinkSupported.value && outputs.value.length
						? "Choose an output exposed by your browser. Earpiece routing is available only if listed."
						: "This browser cannot select an audio output. Using the browser/system default; change output in your device settings.";
			} catch {
				if (callEpoch !== epoch) {
					return;
				}

				outputs.value = [];
				outputNotice.value =
					"Audio outputs could not be listed. Using the browser/system default.";
			}
		};

		const switchOutput = async (id: string) => {
			const current = audio;
			const callEpoch = epoch;

			if (!current?.setSinkId) {
				return false;
			}

			try {
				await current.setSinkId(id === "default" ? "" : id);

				if (callEpoch !== epoch) {
					return false;
				}

				previousOutput = selectedOutput.value;
				selectedOutput.value = id === "default" ? "" : id;
				const selected = outputs.value.find((device) => device.deviceId === id);
				const route = resolvePlaybackRoute(id);
				alternateOutput.value =
					playbackOutputs.value.findIndex(
						(device) => (device.deviceId === "default" ? "" : device.deviceId) === route
					) > 0;
				routeLabel.value = selected
					? selected.label || `Audio output ${outputs.value.indexOf(selected) + 1}`
					: "Browser default";
				outputNotice.value = `Playback: ${routeLabel.value}`;
				return true;
			} catch {
				if (callEpoch !== epoch) {
					return false;
				}

				outputNotice.value = `Output switch failed. Playback remains on ${routeLabel.value}; check device permissions.`;
				return false;
			}
		};

		const toggleOutputs = async () => {
			if (outputBusy.value || !inCall.value) {
				return;
			}

			const callEpoch = epoch;
			outputBusy.value = true;
			showOutputs.value = true;
			// Retry queued playback synchronously while the Audio click's user activation is live.
			playNext();

			try {
				await refreshOutputs();

				if (callEpoch !== epoch || !inCall.value) {
					return;
				}

				if (!sinkSupported.value || !outputs.value.length) {
					return;
				}

				const currentRoute = resolvePlaybackRoute(selectedOutput.value);
				const previousRoute = resolvePlaybackRoute(previousOutput);
				const alternatives = playbackOutputs.value.filter(
					(device) =>
						(device.deviceId === "default" ? "" : device.deviceId) !== currentRoute
				);

				if (playbackOutputs.value.length < 2 || !alternatives.length) {
					outputNotice.value =
						"No alternate playback output is exposed. Use the picker or your browser/system device settings.";
					return;
				}

				const next =
					alternatives.find(
						(device) =>
							(device.deviceId === "default" ? "" : device.deviceId) === previousRoute
					) || alternatives[0];

				if (next) {
					await switchOutput(next.deviceId);
				}
			} finally {
				if (callEpoch === epoch) {
					outputBusy.value = false;
				}
			}
		};

		const selectOutput = async (event: Event) => {
			if (outputBusy.value) {
				return;
			}

			const select = event.target as HTMLSelectElement;
			const callEpoch = epoch;
			outputBusy.value = true;

			try {
				if (!(await switchOutput(select.value)) && callEpoch === epoch) {
					select.value = selectedOutput.value;
				}
			} finally {
				if (callEpoch === epoch) {
					outputBusy.value = false;
				}
			}
		};

		const startRecording = () => {
			if (!stream || muted.value || !inCall.value) {
				return;
			}

			const generation = uploadEpoch;
			const current = ws;
			const mime =
				["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"].find(
					(candidate) => MediaRecorder.isTypeSupported(candidate)
				) || "";
			recorder = recordVoiceSegments(
				stream,
				mime,
				(blob) => {
					if (generation !== uploadEpoch || muted.value || !inCall.value) {
						return;
					}

					void blob
						.arrayBuffer()
						.then((buffer) => {
							if (
								generation === uploadEpoch &&
								ws === current &&
								current &&
								inCall.value &&
								!muted.value
							) {
								current.send(buffer);
							}
						})
						.catch(() => {
							if (generation === uploadEpoch) {
								teardown("Microphone audio could not be read. Retry the call.");
							}
						});
				},
				2000,
				{
					canFinalize: () => performance.now() - lastSpeechAt >= 350,
					onError(recordingError) {
						if (generation === uploadEpoch) {
							teardown(recordingError.message);
						}
					},
				}
			);
		};

		const toggleMute = () => {
			if (!inCall.value) {
				return;
			}

			muted.value = !muted.value;
			stream?.getAudioTracks().forEach((track) => {
				track.enabled = !muted.value;
			});
			stopRecording();

			if (!muted.value) {
				try {
					startRecording();
				} catch {
					teardown("Microphone recording could not restart.");
				}
			}

			if (ws) {
				ws.send({type: "mute", muted: muted.value});
			}
		};

		const startMeter = (media: MediaStream) => {
			const AC =
				window.AudioContext ||
				(window as Window & {webkitAudioContext?: typeof AudioContext}).webkitAudioContext;

			if (!AC) {
				return;
			}

			audioCtx = new AC();
			const source = audioCtx!.createMediaStreamSource(media);
			const analyser = audioCtx!.createAnalyser();
			analyser.fftSize = 2048;
			source.connect(analyser);
			const samples = new Uint8Array(analyser.fftSize);
			const callEpoch = epoch;

			const measure = () => {
				if (callEpoch !== epoch || !inCall.value) {
					return;
				}

				analyser.getByteTimeDomainData(samples);
				let sum = 0;

				for (const sample of samples) {
					sum += ((sample - 128) / 128) ** 2;
				}

				const rms = Math.sqrt(sum / samples.length);

				if (!muted.value && rms > 0.02) {
					lastSpeechAt = performance.now();
				}

				if (
					!muted.value &&
					playing &&
					rms > 0.08 &&
					performance.now() - playStartedAt > 500
				) {
					audio?.pause();
					playing = false;
					queue = [];
					discardThrough = ttsSeq;
				}

				frame = requestAnimationFrame(measure);
			};

			measure();
		};

		const startCall = async (channel = props.channel) => {
			if (inCall.value || connecting.value) {
				minimized.value = false;
				panelOpen.value = true;
				return;
			}

			if (!channel) {
				return;
			}

			target.value = channel;
			agentName.value = "";
			agentRoom.value = "";
			panelOpen.value = true;
			minimized.value = false;
			error.value = "";

			if (store.state.serverConfiguration?.voiceCallSidecarUrl !== "socket.io:/call") {
				error.value =
					"Voice calls are not configured on the host. Run mercury setup stt on the mLounge host and mercury setup on the MIRC host, then retry.";
				return;
			}

			if (
				!store.state.isConnected ||
				!store.state.networks.some((network) =>
					network.channels.some((candidate) => candidate.id === channel.id)
				)
			) {
				error.value = "Reconnect and sign in to mLounge before calling this contact.";
				return;
			}

			if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
				error.value =
					"Microphone recording is unavailable. Open mLounge over HTTPS in a browser supporting MediaRecorder.";
				return;
			}

			connecting.value = true;
			const callEpoch = ++epoch;

			try {
				const acquired = await navigator.mediaDevices.getUserMedia({audio: true});

				if (callEpoch !== epoch) {
					acquired.getTracks().forEach((track) => track.stop());
					return;
				}

				if (
					!store.state.isConnected ||
					!store.state.networks.some((network) =>
						network.channels.some((candidate) => candidate.id === channel.id)
					)
				) {
					acquired.getTracks().forEach((track) => track.stop());
					teardown("Reconnect and sign in to mLounge before calling this contact.");
					return;
				}

				stream = acquired;
				const attemptId = ++nextAttempt;
				let callId = "";
				// eslint-disable-next-line prefer-const -- Handlers capture the connection before registration.
				let current: CallConnection;
				const mime =
					[
						"audio/webm;codecs=opus",
						"audio/webm",
						"audio/ogg;codecs=opus",
						"audio/mp4",
					].find((candidate) => MediaRecorder.isTypeSupported(candidate)) || "";

				const onFrame = (payload: unknown) => {
					if (
						ws !== current ||
						callEpoch !== epoch ||
						!payload ||
						typeof payload !== "object" ||
						Array.isArray(payload)
					) {
						return;
					}

					const message = payload as Record<string, unknown>;

					if (message.attemptId !== attemptId) {
						return;
					}

					if (typeof message.callId === "string" && message.callId) {
						if (callId && message.callId !== callId) {
							return;
						}

						callId = message.callId;
					} else if (!["error", "refused"].includes(String(message.type))) {
						return;
					}

					switch (message.type) {
						case "ready":
							if (!connecting.value) {
								break;
							}

							if (message.engine !== "hermes") {
								teardown(
									"Voice calls are supported only for Hermes agents, not OMP."
								);
								break;
							}

							connecting.value = false;
							agentName.value =
								typeof message.agentName === "string"
									? message.agentName.trim()
									: "";
							agentRoom.value =
								typeof message.agentRoom === "string" ? message.agentRoom : "";
							inCall.value = true;
							establishedAt = Date.now();
							timer = window.setInterval(() => {
								elapsed.value = Math.floor((Date.now() - establishedAt) / 1000);
							}, 250);
							lastSeenId = 0;

							for (const prior of channel.messages) {
								lastSeenId = Math.max(lastSeenId, prior.id || 0);
							}

							ttsSeq = discardThrough = 0;
							selectedOutput.value = "";
							audio = new Audio();
							sinkSupported.value = typeof audio.setSinkId === "function";

							try {
								startRecording();
								startMeter(stream!);
							} catch {
								teardown("Microphone recorder failed to start.");
							}

							break;
						case "refused":
							teardown(String(message.reason || "Call refused"));
							break;
						case "ended":
							teardown();
							break;
						case "error":
							teardown(String(message.message || "Call error"));
							break;
						case "transcript":
							if (
								inCall.value &&
								!muted.value &&
								store.state.isConnected &&
								String(message.text || "").trim()
							) {
								socket.emit("input", {
									target: channel.id,
									text: String(message.text).trim(),
								});
							}

							break;
						case "audio":
							if (
								inCall.value &&
								message.dataUrl &&
								Number(String(message.token || "").replace(/^reply-/, "")) >
									discardThrough
							) {
								queue.push(String(message.dataUrl));
								playNext();
							}

							break;
					}
				};

				const onDisconnect = () => {
					if (ws === current) {
						teardown("Call connection lost. Reconnect to mLounge and retry.");
					}
				};

				const onAuthFailed = () => {
					if (ws === current) {
						teardown(
							"Call authentication expired or was refused. Sign in to mLounge again."
						);
					}
				};

				current = {
					send(packet) {
						if (
							ws !== current ||
							!store.state.isConnected ||
							(!callId &&
								!(
									typeof packet === "object" &&
									"type" in packet &&
									packet.type === "hangup"
								))
						) {
							return;
						}

						relay.emit(
							"voice:call",
							packet instanceof ArrayBuffer
								? {type: "audio", callId, attemptId, data: packet}
								: {...packet, attemptId, ...(callId ? {callId} : {})}
						);
					},
					close() {
						relay.off("voice:call", onFrame);
						relay.off("disconnect", onDisconnect);
						relay.off("auth:failed", onAuthFailed);
					},
				};
				ws = current;
				relay.on("voice:call", onFrame);
				relay.on("disconnect", onDisconnect);
				relay.on("auth:failed", onAuthFailed);
				relay.emit("voice:call", {type: "hello", target: channel.id, mime, attemptId});
			} catch (cause) {
				if (callEpoch === epoch) {
					const name =
						cause && typeof cause === "object" && "name" in cause
							? String(cause.name)
							: "";
					teardown(
						name === "NotAllowedError" || name === "SecurityError"
							? "Microphone permission denied. Allow microphone access for this mLounge site and retry."
							: name === "NotFoundError"
							? "No microphone is available. Connect a microphone and retry."
							: "Could not start microphone recording. Check your browser audio device and retry."
					);
				}
			}
		};

		const retryCall = () => void startCall(target.value);

		const onPhone = (channel: ClientChan) => {
			void startCall(channel);
		};

		eventbus.on("voice-call:start", onPhone);
		watch(
			() => {
				const network = store.state.networks.find((candidate) =>
					candidate.channels.some((channel) => channel.id === target.value?.id)
				);
				const room = agentRoom.value.toLowerCase();
				return [
					target.value?.id,
					room,
					!!network,
					!!room &&
						!!network?.channels.some(
							(channel) =>
								channel.type === ChanType.CHANNEL &&
								channel.name.toLowerCase() === room
						),
				] as const;
			},
			(
				[targetId, room, targetPresent, roomPresent],
				[previousTargetId, previousRoom, targetWasPresent, roomWasPresent]
			) => {
				if (
					((targetId === previousTargetId && targetWasPresent && !targetPresent) ||
						(room === previousRoom && roomWasPresent && !roomPresent)) &&
					(inCall.value || connecting.value)
				) {
					hangup();
				}
			}
		);
		watch(
			() => target.value?.messages.at(-1)?.id,
			() => {
				if (!inCall.value || !target.value || !ws || !store.state.isConnected) {
					return;
				}

				for (const message of target.value.messages) {
					if (
						message.id > lastSeenId &&
						!message.self &&
						message.mercuryKind === "assistant_reply" &&
						(message.type === undefined || String(message.type) === "message")
					) {
						const text = String(message.text || "")
							.replace(/\x03\d{0,2}(,\d{0,2})?/g, "")
							.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
							.trim();

						if (text) {
							ws.send({type: "tts", text, token: `reply-${++ttsSeq}`});
						}
					}
				}

				for (const message of target.value.messages) {
					lastSeenId = Math.max(lastSeenId, message.id || 0);
				}
			}
		);
		onBeforeUnmount(() => {
			eventbus.off("voice-call:start", onPhone);
			teardown();
		});
		return {
			inCall,
			connecting,
			panelOpen,
			minimized,
			muted,
			error,
			contactName,
			duration,
			showOutputs,
			sinkSupported,
			outputs,
			selectedOutput,
			alternateOutput,
			outputBusy,
			routeLabel,
			outputNotice,
			playbackError,
			startCall,
			retryCall,
			hangup,
			toggleMute,
			toggleOutputs,
			selectOutput,
		};
	},
});
</script>
