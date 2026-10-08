import {expect, it} from "vitest";
import {createServer} from "http";
import {Server} from "socket.io";
import {io} from "socket.io-client";
import {WebSocketServer} from "ws";
import {registerVoiceCall, loadVoiceRelayConfig} from "../../server/voice-call";
import type {VoiceCallFrame} from "../../shared/types/socket-events";
import {ChanType, ChanState} from "../../shared/types/chan";

it("relays retained call wire over authenticated socket with server-only credentials", async () => {
	const sidecar = new WebSocketServer({port: 0, host: "127.0.0.1"});
	await new Promise<void>((resolve) => sidecar.once("listening", resolve));
	const address = sidecar.address();

	if (typeof address === "string") {
		throw new Error("expected TCP");
	}

	const received: (string | Buffer)[] = [];
	sidecar.on("connection", (ws, request) => {
		expect(request.headers["x-voice-call-token"]).toBe("private-service-secret");
		expect(request.url).toBe("/call");
		ws.on("message", (data, isBinary) => {
			if (isBinary) {
				received.push(Buffer.from(data as Buffer));
				ws.send(JSON.stringify({type: "transcript", callId: "call-owned", text: "hello"}));
				return;
			}

			const bytes = Buffer.isBuffer(data)
				? data
				: Array.isArray(data)
				? Buffer.concat(data)
				: Buffer.from(data);
			const frame = JSON.parse(bytes.toString("utf8")) as VoiceCallFrame;
			received.push(bytes.toString("utf8"));

			if (frame.type === "hello") {
				ws.send(
					JSON.stringify({
						type: "ready",
						callId: "call-owned",
						engine: "hermes",
						sttProvider: "parakeet",
					})
				);
			}

			if (frame.type === "tts") {
				ws.send(
					JSON.stringify({
						type: "audio",
						callId: "call-owned",
						token: frame.token,
						dataUrl: "data:audio/wav;base64,UklGRg==",
					})
				);
			}
		});
	});
	const cfg = {
		origin: "https://lounge.example",
		users: ["owner"],
		sidecarUrl: `http://127.0.0.1:${address.port}`,
		sidecarToken: "private-service-secret",
		network: {uuid: "owned-network", host: "irc.example", port: 6697, tls: true},
		profile: "voice",
	};
	const target = {
		chan: {id: 123, name: "#voice", type: ChanType.CHANNEL, state: ChanState.JOINED},
		network: {
			uuid: "owned-network",
			host: "irc.example",
			port: 6697,
			tls: true,
			status: {connected: true},
			irc: {options: {host: "irc.example", port: 6697, tls: true}},
		},
	};
	const client = {
		name: "owner",
		config: {sessions: {session: {}}},
		find: (id: number) => (id === 123 ? target : null),
	};
	const http = createServer();
	const server = new Server(http, {maxHttpBufferSize: 8 * 1024 * 1024 + 65536});
	server.on("connection", (socket) => {
		// The production seam is initializeClient after performAuthentication;
		// this isolated authenticator avoids real accounts and provider secrets.
		socket.on("auth:perform", (value: unknown) => {
			if (value === "private-user") {
				registerVoiceCall(socket, client, "session", () => cfg);
			}
		});
	});
	await new Promise<void>((resolve) => http.listen(0, "127.0.0.1", resolve));
	const listen = http.address();

	if (!listen || typeof listen === "string") {
		throw new Error("expected TCP");
	}

	const browser = io(`http://127.0.0.1:${listen.port}`, {
		transports: ["websocket"],
		extraHeaders: {Origin: cfg.origin},
		reconnection: false,
	});
	const nextFrame = () =>
		new Promise<VoiceCallFrame>((resolve, reject) => {
			const timeout = setTimeout(() => reject(new Error("frame timeout")), 2000);
			browser.once("voice:call", (frame: VoiceCallFrame) => {
				clearTimeout(timeout);
				resolve(frame);
			});
		});

	try {
		await new Promise<void>((resolve) => browser.once("connect", resolve));
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		browser.emit("auth:perform", "private-user");
		let response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 999, mime: "audio/wav"});
		expect((await response).type).toBe("refused");
		response = nextFrame();
		browser.emit("voice:call", {
			type: "hello",
			target: 123,
			mime: "audio/wav",
			url: "http://evil",
			profile: "foreign",
			provider: "openai",
		});
		expect((await response).type).toBe("refused");
		expect(received).toHaveLength(0);
		client.name = "foreign-user";
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("refused");
		client.name = "owner";
		target.network.irc.options.host = "foreign-irc";
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("refused");
		target.network.irc.options.host = "irc.example";
		cfg.origin = "https://foreign-origin.example";
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("refused");
		cfg.origin = "https://lounge.example";
		target.network.uuid = "foreign-network";
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("refused");
		target.network.uuid = "owned-network";
		expect(received).toHaveLength(0);
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("ready");
		response = nextFrame();
		browser.emit("voice:call", {type: "audio", callId: "foreign", data: Buffer.from("audio")});
		expect((await response).type).toBe("error");
		response = nextFrame();
		browser.emit("voice:call", {
			type: "audio",
			callId: "call-owned",
			data: Buffer.from("audio"),
		});
		expect((await response).text).toBe("hello");
		response = nextFrame();
		browser.emit("voice:call", {
			type: "tts",
			callId: "call-owned",
			text: "reply",
			token: "reply-1",
		});
		expect((await response).type).toBe("audio");
		response = nextFrame();
		browser.emit("voice:call", {type: "hangup", callId: "call-owned"});
		expect((await response).type).toBe("ended");
		expect(received.some((value) => Buffer.isBuffer(value))).toBe(true);
		response = nextFrame();
		browser.emit("voice:call", {type: "hangup", callId: "call-owned"});
		expect((await response).type).toBe("error");
		Reflect.deleteProperty(client.config.sessions, "session");
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("refused");
		Reflect.set(client.config.sessions, "session", {});
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("ready");
		response = nextFrame();
		browser.emit("voice:call", {
			type: "audio",
			callId: "call-owned",
			data: Buffer.alloc(8 * 1024 * 1024 + 1),
		});
		expect((await response).type).toBe("error");
		expect(received.filter((value) => Buffer.isBuffer(value))).toHaveLength(1);
		response = nextFrame();
		browser.emit("voice:call", {type: "hello", target: 123, mime: "audio/wav"});
		expect((await response).type).toBe("ready");
		response = nextFrame();
		target.network.status.connected = false;
		expect((await response).type).toBe("ended");
	} finally {
		browser.close();
		await new Promise<void>((resolve) => server.close(() => resolve()));

		for (const ws of sidecar.clients) {
			ws.terminate();
		}

		await new Promise<void>((resolve) => sidecar.close(() => resolve()));
	}
});
