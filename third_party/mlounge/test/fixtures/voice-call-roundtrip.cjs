// Private offline integration: real mLounge password/session auth -> compiled relay -> canonical tools.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {once} = require("node:events");
const {io} = require("socket.io-client");
const bcrypt = require("bcryptjs");
const {loadVoiceRelayConfig} = require("../../dist/server/voice-call.js");
const Config = require("../../dist/server/config.js").default;
const Client = require("../../dist/server/client.js").default;
const start = require("../../dist/server/server.js").default;

(async () => {
	const config = loadVoiceRelayConfig();
	assert(config, "Setup private bootstrap not readable");
	const home = process.env.FIXTURE_MLOUNGE_HOME;
	fs.mkdirSync(path.join(home, "users"), {recursive: true});
	fs.mkdirSync(path.join(home, "packages"), {recursive: true});
	fs.writeFileSync(
		path.join(home, "packages/package.json"),
		JSON.stringify({name: "private-voice-fixture", private: true, dependencies: {}})
	);
	fs.writeFileSync(
		path.join(home, "config.js"),
		"module.exports={host:'127.0.0.1',port:0,public:false,identd:{enable:false},prefetch:false,transports:['websocket']};"
	);
	fs.writeFileSync(
		path.join(home, "users/owner.json"),
		JSON.stringify({
			password: bcrypt.hashSync("fixture-login-password", 4),
			sessions: {},
			networks: [],
			log: false,
		})
	);
	Config.setHome(home);
	const target = {
		chan: {id: 123, name: "#voice", type: "channel", state: 1},
		network: {
			uuid: "owned",
			...config.network,
			status: {connected: true},
			irc: {options: {...config.network}},
		},
	};
	const originalFind = Client.prototype.find;

	// Only the offline IRC membership boundary is injected. Password authentication,
	// fresh session generation, session revocation, and relay registration are production code.
	Client.prototype.find = function (id) {
		return id === 123 ? target : originalFind.call(this, id);
	};

	const server = await start({dev: false});

	if (!server.listening) {await once(server, "listening");}

	const browser = io(`http://127.0.0.1:${server.address().port}`, {
		transports: ["websocket"],
		extraHeaders: {Origin: config.origin},
		reconnection: false,
		autoConnect: false,
	});

	const frame = async (data) => {
		const reply = once(browser, "voice:call", {signal: AbortSignal.timeout(5000)});
		browser.emit("voice:call", data);
		return (await reply)[0];
	};

	try {
		const authenticate = once(browser, "auth:start", {signal: AbortSignal.timeout(5000)});
		browser.open();
		await authenticate;
		const initialized = once(browser, "init", {signal: AbortSignal.timeout(5000)});
		browser.emit("auth:perform", {user: "owner", password: "fixture-login-password"});
		const [init] = await initialized;
		assert(
			init.token,
			"fresh browser must receive existing mLounge session token, not a voice credential"
		);
		const ready = await frame({type: "hello", target: 123, mime: "audio/wav"});
		assert.equal(ready.type, "ready", JSON.stringify(ready));
		assert.equal(ready.sttProvider, "parakeet");
		const transcript = await frame({
			type: "audio",
			callId: ready.callId,
			data: fs.readFileSync(process.env.FIXTURE_AUDIO),
		});
		assert.equal(transcript.text, "bonjour from browser");
		const speech = await frame({
			type: "tts",
			callId: ready.callId,
			text: "Hermes reply to " + transcript.text,
			token: "reply-1",
		});
		assert.equal(speech.type, "audio");
		assert.equal(speech.provider, process.env.FIXTURE_TTS_PROVIDER);
		const bytes = Buffer.from(speech.dataUrl.split(",")[1], "base64");
		assert.equal(bytes.subarray(0, 4).toString(), "RIFF");
		assert.equal(bytes.readUInt32LE(40), 3200);
		const ended = await frame({type: "hangup", callId: ready.callId});
		assert.equal(ended.type, "ended");
		process.stdout.write(
			"VOICE_CALL_PROOF=" +
				JSON.stringify({
					freshPasswordLogin: true,
					ready: true,
					transcript: transcript.text,
					provider: speech.provider,
					audioBytes: bytes.length,
					ended: true,
				}) +
				"\n"
		);
	} finally {
		browser.close();
		Client.prototype.find = originalFind;
		await new Promise((resolve) => server.close(resolve));
	}
})()
	.then(() => process.exit(0))
	.catch((error) => {
		process.stderr.write(String(error) + "\n");
		process.exit(1);
	});
