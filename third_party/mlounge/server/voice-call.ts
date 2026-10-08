import fs from "fs";
import {randomUUID} from "crypto";
import WebSocket from "ws";
import {ChanState, ChanType} from "../shared/types/chan";
import type {VoiceCallFrame} from "../shared/types/socket-events";

const MAX_AUDIO = 8 * 1024 * 1024;
export type VoiceRelayConfig = {
	origin: string;
	sidecarUrl: string;
	sidecarToken: string;
	profile: string;
	users: string[];
	network: {uuid: string; host: string; port: number; tls: boolean};
};
type Target = {
	chan: {id: number; name: string; type: ChanType; state: ChanState};
	network: {
		uuid: string;
		host: string;
		port: number;
		tls: boolean;
		status: {connected: boolean};
		irc?: {options?: {host: string; port: number; tls: boolean}};
	};
};
export type VoiceClient = {
	name: string;
	config: {sessions: Record<string, unknown>};
	find: (id: number) => Target | null | false;
};
export type VoiceSocket = {
	connected: boolean;
	request: {headers: {origin?: string}};
	on: {
		(event: "voice:call", listener: (data: unknown) => void): unknown;
		(event: "disconnect", listener: () => void): unknown;
	};
	emit: (event: "voice:call", frame: VoiceCallFrame) => unknown;
};
type ActiveCall = {
	peer: WebSocket;
	target: number;
	network: string;
	channel: string;
	callId: string;
	upstreamId: string;
	attemptId: number;
	config: VoiceRelayConfig;
	timer: NodeJS.Timeout | null;
};

function object(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function loadVoiceRelayConfig(): VoiceRelayConfig | null {
	const file = process.env.MERCURY_VOICE_CALL_CONFIG;

	if (!file) {
		return null;
	}

	try {
		const stat = fs.statSync(file);

		if (!stat.isFile() || (stat.mode & 0o077) !== 0 || stat.uid !== process.getuid?.()) {
			return null;
		}

		const data: unknown = JSON.parse(fs.readFileSync(file, "utf8"));

		if (
			!object(data) ||
			!object(data.network) ||
			typeof data.origin !== "string" ||
			typeof data.sidecarUrl !== "string" ||
			typeof data.envPath !== "string" ||
			typeof data.profile !== "string" ||
			!Array.isArray(data.users) ||
			!data.users.every((user: unknown) => typeof user === "string") ||
			typeof data.network.uuid !== "string" ||
			!data.network.uuid ||
			typeof data.network.host !== "string" ||
			typeof data.network.port !== "number" ||
			typeof data.network.tls !== "boolean"
		) {
			return null;
		}

		const envStat = fs.statSync(data.envPath);

		if (
			!envStat.isFile() ||
			(envStat.mode & 0o077) !== 0 ||
			envStat.uid !== process.getuid?.()
		) {
			return null;
		}

		const line = fs
			.readFileSync(data.envPath, "utf8")
			.split(/\r?\n/)
			.find((entry) => /^(?:export\s+)?VOICE_CALL_SIDECAR_TOKEN=/.test(entry.trim()));

		if (!line) {
			return null;
		}

		let sidecarToken = line.slice(line.indexOf("=") + 1).trim();

		if (sidecarToken.startsWith('"')) {
			const parsed: unknown = JSON.parse(sidecarToken);

			if (typeof parsed !== "string") {
				return null;
			}

			sidecarToken = parsed;
		} else if (sidecarToken.startsWith("'") && sidecarToken.endsWith("'")) {
			sidecarToken = sidecarToken.slice(1, -1);
		}

		if (!sidecarToken || /[\r\n]/.test(sidecarToken)) {
			return null;
		}

		const origin = new URL(data.origin);
		const peer = new URL(data.sidecarUrl);

		if (
			origin.origin !== data.origin ||
			!["http:", "https:"].includes(peer.protocol) ||
			peer.username ||
			peer.password ||
			peer.search ||
			peer.hash ||
			peer.pathname !== "/"
		) {
			return null;
		}

		return {
			origin: data.origin,
			sidecarUrl: data.sidecarUrl,
			sidecarToken,
			profile: data.profile,
			users: data.users,
			network: {
				uuid: data.network.uuid,
				host: data.network.host,
				port: data.network.port,
				tls: data.network.tls,
			},
		};
	} catch {
		return null;
	}
}

// Called ONLY by initializeClient after the existing private authenticator.
export function registerVoiceCall(
	socket: VoiceSocket,
	client: VoiceClient,
	session: string,
	readConfig = loadVoiceRelayConfig
): void {
	let active: ActiveCall | null = null;
	let lastAttempt = 0;
	const send = (frame: VoiceCallFrame) => socket.emit("voice:call", frame);

	const close = () => {
		const call = active;
		active = null;

		if (!call) {
			return;
		}

		if (call.timer) {
			clearInterval(call.timer);
		}

		if (call.peer.readyState === WebSocket.OPEN) {
			if (call.upstreamId) {
				call.peer.send(JSON.stringify({type: "hangup", callId: call.upstreamId}));
			}

			call.peer.close();
		} else {
			call.peer.terminate();
		}
	};

	const targetAllowed = (id: number, cfg: VoiceRelayConfig): Target | null => {
		if (
			!socket.connected ||
			!session ||
			!Object.hasOwn(client.config.sessions, session) ||
			!cfg.users.includes(client.name) ||
			socket.request.headers.origin !== cfg.origin
		) {
			return null;
		}

		const target = client.find(id);

		if (
			!target ||
			!target.network.status.connected ||
			(target.chan.type !== ChanType.QUERY &&
				(target.chan.type !== ChanType.CHANNEL || target.chan.state !== ChanState.JOINED))
		) {
			return null;
		}

		const options = target.network.irc?.options;

		if (
			!options ||
			target.network.uuid !== cfg.network.uuid ||
			target.network.host !== cfg.network.host ||
			target.network.port !== cfg.network.port ||
			target.network.tls !== cfg.network.tls ||
			options.host !== cfg.network.host ||
			options.port !== cfg.network.port ||
			options.tls !== cfg.network.tls
		) {
			return null;
		}

		return target;
	};

	socket.on("voice:call", (data: unknown) => {
		if (!object(data) || typeof data.type !== "string") {
			return;
		}

		if (data.type === "hello") {
			const attemptId = data.attemptId;

			if (
				typeof attemptId !== "number" ||
				!Number.isSafeInteger(attemptId) ||
				attemptId <= lastAttempt
			) {
				send({
					type: "refused",
					attemptId: typeof attemptId === "number" ? attemptId : undefined,
					reason: "Invalid or replayed call attempt",
				});
				return;
			}

			if (active) {
				send({type: "refused", attemptId, reason: "End the existing call first"});
				return;
			}

			const cfg = readConfig();
			const target =
				cfg && typeof data.target === "number" && Number.isSafeInteger(data.target)
					? targetAllowed(data.target, cfg)
					: null;

			if (!cfg || !target) {
				send({
					type: "refused",
					attemptId,
					reason: "Call target/session unavailable; rerun Mercury Setup on the selected host/profile",
				});
				return;
			}

			if (
				Object.keys(data).some(
					(key) => !["type", "target", "mime", "attemptId"].includes(key)
				) ||
				typeof data.mime !== "string" ||
				!/^audio\/(webm|ogg|mp4|mpeg|wav)(;.*)?$/.test(data.mime)
			) {
				send({type: "refused", attemptId, reason: "Invalid call configuration"});
				return;
			}

			lastAttempt = attemptId;

			const url = new URL(cfg.sidecarUrl);
			url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
			url.pathname = "/call";
			const peer = new WebSocket(url, {
				headers: {"X-Voice-Call-Token": cfg.sidecarToken},
				maxPayload: 16 * 1024 * 1024,
				followRedirects: false,
				handshakeTimeout: 5000,
			});
			const call: ActiveCall = {
				peer,
				target: target.chan.id,
				network: target.network.uuid,
				channel: target.chan.name,
				callId: randomUUID(),
				upstreamId: "",
				attemptId,
				config: cfg,
				timer: null,
			};
			active = call;
			peer.on("open", () => {
				if (active !== call) {
					return;
				}

				if (!targetAllowed(call.target, cfg)) {
					close();
					return;
				}

				peer.send(JSON.stringify({type: "hello", channel: call.channel, mime: data.mime}));
				call.timer = setInterval(() => {
					if (active !== call) {
						return;
					}

					const latest = readConfig();
					const context =
						latest && JSON.stringify(latest) === JSON.stringify(cfg)
							? targetAllowed(call.target, latest)
							: null;

					if (
						!context ||
						context.network.uuid !== call.network ||
						context.chan.name !== call.channel
					) {
						close();
						send({type: "ended", callId: call.callId, attemptId});
					}
				}, 1000);
			});
			peer.on("message", (raw) => {
				if (active !== call) {
					return;
				}

				const current = targetAllowed(call.target, cfg);

				if (
					!current ||
					current.network.uuid !== call.network ||
					current.chan.name !== call.channel
				) {
					close();
					return;
				}

				try {
					const bytes = Buffer.isBuffer(raw)
						? raw
						: Array.isArray(raw)
						? Buffer.concat(raw)
						: Buffer.from(raw);
					const frame: unknown = JSON.parse(bytes.toString("utf8"));

					if (!object(frame) || typeof frame.type !== "string") {
						close();
						return;
					}

					if (frame.type === "ready") {
						if (
							frame.engine !== "hermes" ||
							typeof frame.callId !== "string" ||
							!frame.callId
						) {
							close();
							send({
								type: "refused",
								reason: "Only a confirmed Hermes call is allowed",
								attemptId,
							});
							return;
						}

						if (call.upstreamId) {
							close();
							return;
						}

						call.upstreamId = frame.callId;
					} else if (frame.callId !== undefined && frame.callId !== call.upstreamId) {
						close();
						return;
					}

					send({...frame, type: frame.type, callId: call.callId, attemptId});
				} catch {
					close();
					// Every frame is scoped, including service errors without an upstream ID.
					send({type: "error", attemptId, message: "Invalid voice service response"});
				}
			});
			peer.on("error", () => {
				if (active === call) {
					close();
					send({
						type: "error",
						attemptId,
						message: "Configured voice service unavailable; rerun host Setup/doctor",
					});
				}
			});
			peer.on("close", () => {
				if (call.timer) {
					clearInterval(call.timer);
				}

				if (active === call) {
					active = null;
					send({type: "ended", callId: call.callId, attemptId});
				}
			});
			return;
		}

		const call = active;

		if (
			!call ||
			data.attemptId !== call.attemptId ||
			(data.type !== "hangup" && (!call.upstreamId || data.callId !== call.callId)) ||
			(data.type === "hangup" && data.callId !== undefined && data.callId !== call.callId)
		) {
			send({
				type: "error",
				attemptId: typeof data.attemptId === "number" ? data.attemptId : undefined,
				message: "Invalid or expired call ID",
			});
			return;
		}

		const cfg = readConfig();
		const target =
			cfg && JSON.stringify(cfg) === JSON.stringify(call.config)
				? targetAllowed(call.target, cfg)
				: null;

		if (!target || target.network.uuid !== call.network || target.chan.name !== call.channel) {
			close();
			send({type: "error", attemptId: call.attemptId, message: "Call authorization expired"});
			return;
		}

		if (data.type === "hangup") {
			close();
			send({type: "ended", callId: call.callId, attemptId: call.attemptId});
			return;
		}

		if (call.peer.readyState !== WebSocket.OPEN || call.peer.bufferedAmount > MAX_AUDIO) {
			close();
			return;
		}

		if (data.type === "audio") {
			const audio = data.data;

			if (
				!(audio instanceof Uint8Array) ||
				!audio.byteLength ||
				audio.byteLength > MAX_AUDIO
			) {
				close();
				send({
					type: "error",
					attemptId: call.attemptId,
					message: "Invalid or oversized audio",
				});
				return;
			}

			call.peer.send(audio);
		} else if (
			data.type === "tts" &&
			typeof data.text === "string" &&
			data.text.length > 0 &&
			data.text.length <= 16000 &&
			typeof data.token === "string" &&
			data.token.length <= 128
		) {
			call.peer.send(
				JSON.stringify({
					type: "tts",
					callId: call.upstreamId,
					text: data.text,
					token: data.token,
				})
			);
		} else if (data.type === "mute" && typeof data.muted === "boolean") {
			call.peer.send(
				JSON.stringify({type: "mute", callId: call.upstreamId, muted: data.muted})
			);
		} else if (data.type === "ping") {
			call.peer.send(JSON.stringify({type: "ping", callId: call.upstreamId}));
		} else {
			close();
			send({type: "error", attemptId: call.attemptId, message: "Invalid call control"});
		}
	});
	socket.on("disconnect", close);
}
