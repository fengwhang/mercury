// @vitest-environment jsdom
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {flushPromises, mount, VueWrapper} from "@vue/test-utils";
import VoiceCall from "../../../client/components/VoiceCall.vue";
import type {ClientChan, ClientNetwork} from "../../../client/js/types";
import eventbus from "../../../client/js/eventbus";

const state = vi.hoisted(() => ({
	isConnected: true,
	serverConfiguration: {voiceCallSidecarUrl: ""},
	networks: [] as ClientNetwork[],
}));
const recordings = vi.hoisted(() => [] as Array<(blob: Blob) => void>);
const emitInput = vi.hoisted(() => vi.fn());
vi.mock("../../../client/js/store", async () => {
	// vi.mock factories run before static imports initialize; load Vue here for the reactive mock.
	const {reactive} = await import("vue");
	state.networks = reactive(state.networks);
	return {useStore: () => ({state})};
});
vi.mock("../../../client/js/helpers/voice-recording", () => ({
	recordVoiceSegments(_stream: MediaStream, _mime: string, onChunk: (blob: Blob) => void) {
		recordings.push(onChunk);
		return {stop: vi.fn()};
	},
}));
const relay = vi.hoisted(() => ({
	listener: null as null | ((value: Record<string, unknown>) => void),
	on: vi.fn(),
	off: vi.fn(),
}));
vi.mock("../../../client/js/socket", () => ({
	default: {emit: emitInput, on: relay.on, off: relay.off},
}));

class FakeSocket {
	static OPEN = 1;
	static instances: unknown[] = [];
	readyState = 1;
	onopen: (() => void) | null = null;
	onmessage: ((event: {data: string}) => void) | null = null;
	onclose: (() => void) | null = null;
	onerror: (() => void) | null = null;
	send = vi.fn();
	close = vi.fn();
	constructor(readonly attemptId: number) {
		const listener = relay.listener;
		this.onmessage = (event) => listener?.(JSON.parse(event.data));
		FakeSocket.instances.push(this);
	}
	message(value: object) {
		this.onmessage?.({
			data: JSON.stringify({
				callId: `fixture-${FakeSocket.instances.indexOf(this)}`,
				attemptId: this.attemptId,
				...value,
			}),
		});
	}
}
// Only FakeSocket constructors register instances.
const sockets = FakeSocket.instances as FakeSocket[];
const getMedia = vi.fn();
const enumerate = vi.fn();

function configureRelay(wrapper: VueWrapper) {
	state.serverConfiguration.voiceCallSidecarUrl = "socket.io:/call";

	if (
		!state.networks.some((network) =>
			network.channels.some((channel) => channel.id === wrapper.props("channel")?.id)
		)
	) {
		state.networks.push({uuid: "mirc", channels: [wrapper.props("channel")]} as ClientNetwork);
	}
}

const stop = vi.fn();
const track = {stop, enabled: true};
const media = {getTracks: () => [track], getAudioTracks: () => [track]};

function render() {
	return mount(VoiceCall, {
		props: {
			channel: {id: 1, name: "#Gaia", messages: []} as unknown as ClientChan,
		},
		attachTo: document.body,
	});
}

const sink = vi.fn().mockResolvedValue(undefined);
const pause = vi.fn();
const play = vi.fn<() => Promise<void>>().mockResolvedValue(undefined);
const playedSources: string[] = [];
class FakeAudio {
	src = "";
	onended: (() => void) | null = null;
	onerror: (() => void) | null = null;
	setSinkId: ((id: string) => Promise<void>) | undefined = sink;
	play() {
		playedSources.push(this.src);
		return play();
	}
	pause = pause;
	removeAttribute = vi.fn();
}

async function connect(wrapper: VueWrapper) {
	configureRelay(wrapper);
	await wrapper.get(".call-toggle").trigger("click");
	await flushPromises();
	sockets[0].onopen?.();
	sockets[0].message({type: "ready", engine: "hermes", agentName: "Gaia"});
	await flushPromises();
}

async function click(selector: string) {
	(document.querySelector(selector) as HTMLButtonElement).click();
	await flushPromises();
}

beforeEach(() => {
	localStorage.clear();
	sockets.length = 0;
	stop.mockClear();
	state.serverConfiguration.voiceCallSidecarUrl = "";
	state.networks.length = 0;
	state.isConnected = true;
	relay.listener = null;
	relay.on.mockImplementation((event, listener) => {
		if (event === "voice:call") {
			relay.listener = listener;
		}
	});
	relay.off.mockImplementation((event) => {
		if (event === "voice:call") {
			sockets.at(-1)?.close();
			relay.listener = null;
		}
	});
	emitInput.mockReset().mockImplementation((event, value) => {
		if (event !== "voice:call") {
			return;
		}

		if (value.type === "hello") {
			new FakeSocket(value.attemptId);
		} else {
			const {callId: _callId, attemptId: _attemptId, ...control} = value;
			sockets.at(-1)?.send(value.type === "audio" ? value.data : JSON.stringify(control));
		}
	});
	recordings.length = 0;
	sink.mockClear();
	pause.mockClear();
	play.mockReset().mockResolvedValue(undefined);
	playedSources.length = 0;
	track.enabled = true;
	getMedia.mockResolvedValue(media);
	enumerate.mockResolvedValue([]);
	vi.stubGlobal("WebSocket", FakeSocket);
	vi.stubGlobal("Audio", FakeAudio);
	vi.stubGlobal("MediaRecorder", {isTypeSupported: () => true});
	vi.stubGlobal("requestAnimationFrame", vi.fn());
	vi.stubGlobal("cancelAnimationFrame", vi.fn());
	Object.defineProperty(navigator, "mediaDevices", {
		configurable: true,
		value: {
			getUserMedia: getMedia,
			enumerateDevices: enumerate,
		},
	});
});
afterEach(() => {
	vi.useRealTimers();
	vi.unstubAllGlobals();
	document.body.innerHTML = "";
});

describe("voice phone entry", () => {
	it("uses authenticated chat transport with numeric target and no browser secrets", async () => {
		localStorage.setItem("mlounge.voiceCall.token", "obsolete-service-secret");
		localStorage.setItem("mlounge.voiceCall.sidecarUrl", "http://localhost:9999");
		const wrapper = render();
		configureRelay(wrapper);
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		expect(document.querySelector("input")).toBeNull();
		expect(emitInput).toHaveBeenCalledWith("voice:call", {
			type: "hello",
			target: 1,
			attemptId: expect.any(Number),
			mime: "audio/webm;codecs=opus",
		});
		expect(localStorage.getItem("mlounge.voiceCall.token")).toBeNull();
		expect(localStorage.getItem("mlounge.voiceCall.sidecarUrl")).toBeNull();
		wrapper.unmount();
	});
	it("releases delayed microphone permission when chat disconnects before it resolves", async () => {
		const wrapper = render();
		configureRelay(wrapper);
		const {promise, resolve} = Promise.withResolvers<MediaStream>();
		getMedia.mockReturnValue(promise);
		await wrapper.get(".call-toggle").trigger("click");
		state.isConnected = false;
		resolve(media as unknown as MediaStream);
		await flushPromises();
		expect(sockets).toHaveLength(0);
		expect(stop).toHaveBeenCalledOnce();
		expect(document.body.textContent).toContain("Reconnect");
		state.isConnected = true;
		wrapper.unmount();
	});
	it("does not acquire a microphone for disconnected or unowned chat targets", async () => {
		const wrapper = render();
		configureRelay(wrapper);
		state.isConnected = false;
		getMedia.mockClear();
		await wrapper.get(".call-toggle").trigger("click");
		expect(getMedia).not.toHaveBeenCalled();
		expect(document.body.textContent).toContain("sign in to mLounge");
		state.isConnected = true;
		state.networks.splice(0);
		await wrapper.get(".call-toggle").trigger("click");
		expect(getMedia).not.toHaveBeenCalled();
		wrapper.unmount();
	});
	it("shows permission denial without credential prompts or a late call start", async () => {
		const wrapper = render();
		configureRelay(wrapper);
		getMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		expect(document.body.textContent).toContain("Microphone permission denied");
		expect(document.querySelector("input")).toBeNull();
		expect(sockets).toHaveLength(0);
		wrapper.unmount();
	});
	it("ignores frames from another call ID after establishment", async () => {
		const wrapper = render();
		await connect(wrapper);
		emitInput.mockClear();
		sockets[0].message({type: "transcript", callId: "stale", text: "wrong"});
		sockets[0].message({type: "ended", callId: "stale"});
		await flushPromises();
		expect(emitInput).not.toHaveBeenCalled();
		expect(document.querySelector(".voice-timer")).not.toBeNull();
		wrapper.unmount();
	});
	it.each(["expired", "forged"])(
		"surfaces %s authentication errors without asking for secrets",
		async (kind) => {
			const wrapper = render();
			configureRelay(wrapper);
			await wrapper.get(".call-toggle").trigger("click");
			await flushPromises();
			sockets[0].message({
				type: "error",
				message: `Call authentication ${kind}. Sign in to mLounge again.`,
			});
			await flushPromises();
			expect(document.body.textContent).toContain(`authentication ${kind}`);
			expect(document.querySelector("input")).toBeNull();
			expect(stop).toHaveBeenCalledOnce();
			wrapper.unmount();
		}
	);
	it("renders the real Mercury thermometer without the pink face in full screen and PiP", async () => {
		const wrapper = render();
		await connect(wrapper);
		expect(document.querySelector(".voice-avatar")?.textContent).toBe("🌡️");
		expect(document.querySelector(".voice-avatar svg")).toBeNull();
		expect(document.querySelector(".voice-call-screen")?.getAttribute("aria-label")).toBe(
			"Mercury voice call with Gaia"
		);
		await click(".voice-pip");
		expect(document.querySelector(".minimized .voice-avatar")?.textContent).toBe("🌡️");
		wrapper.unmount();
	});
	it("shows the registered agent name rather than a prefixed channel slug", async () => {
		const wrapper = render();
		await wrapper.setProps({
			channel: {
				id: 1,
				name: "#network_parent/delegate",
				messages: [],
			} as unknown as ClientChan,
		});
		configureRelay(wrapper);
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		sockets[0].message({type: "ready", engine: "hermes", agentName: "Registered Agent"});
		await flushPromises();
		expect(document.querySelector(".voice-identity h2")?.textContent).toBe("Registered Agent");
		wrapper.unmount();
	});
	it("shows canonical host setup instead of collecting browser credentials when unconfigured", async () => {
		const wrapper = render();
		await wrapper.get(".call-toggle").trigger("click");
		expect(document.body.querySelector(".voice-settings input")).toBeNull();
		expect(document.body.textContent).toContain("mercury setup stt");
		wrapper.unmount();
	});
	it("starts from configured relay and starts the timer only at ready", async () => {
		const wrapper = render();
		configureRelay(wrapper);
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		vi.useFakeTimers();
		await vi.advanceTimersByTimeAsync(5000);
		expect(document.querySelector(".voice-timer")).toBeNull();
		expect(sockets.length, document.body.textContent || "").toBe(1);
		sockets[0].message({type: "ready", engine: "hermes"});
		await flushPromises();
		expect(document.querySelector(".voice-timer")?.textContent).toBe("00:00");
		await vi.advanceTimersByTimeAsync(2000);
		expect(document.querySelector(".voice-timer")?.textContent).toBe("00:02");
		wrapper.unmount();
		vi.useRealTimers();
	});
	it("mutes immediately and discards uploads pending across mute and unmute", async () => {
		const wrapper = render();
		await connect(wrapper);
		const {promise, resolve} = Promise.withResolvers<ArrayBuffer>();
		recordings[0]({arrayBuffer: () => promise} as Blob);
		await click(".voice-mute");
		expect(track.enabled).toBe(false);
		expect(document.querySelector(".voice-mute")?.getAttribute("aria-pressed")).toBe("true");
		expect(
			document.querySelector(".voice-mute")?.parentElement?.querySelector("span")?.textContent
		).toBe("Mute");
		await click(".voice-mute");
		expect(track.enabled).toBe(true);
		resolve(new ArrayBuffer(1));
		await flushPromises();
		expect(sockets[0].send.mock.calls.some(([value]) => value instanceof ArrayBuffer)).toBe(
			false
		);
		sockets[0].message({type: "muted", muted: true});
		await flushPromises();
		expect(document.querySelector(".voice-mute")?.getAttribute("aria-pressed")).toBe("false");
		wrapper.unmount();
	});
	it("ends locally immediately and ignores a queued ready callback", async () => {
		const wrapper = render();
		await connect(wrapper);
		const delayed = sockets[0].onmessage!;
		await click(".voice-end");
		expect(document.querySelector(".voice-call-screen")).toBeNull();
		expect(stop).toHaveBeenCalledOnce();
		expect(sockets[0].close).toHaveBeenCalledOnce();
		expect(sockets[0].send).toHaveBeenCalledWith(JSON.stringify({type: "hangup"}));
		delayed({data: JSON.stringify({type: "ready", engine: "hermes"})});
		await flushPromises();
		expect(document.querySelector(".voice-call-screen")).toBeNull();
		wrapper.unmount();
	});
	it("cancels an acknowledged pending relay before ready without accepting late readiness", async () => {
		const wrapper = render();
		configureRelay(wrapper);
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		sockets[0].message({type: "pong"});
		const delayed = sockets[0].onmessage!;
		await click(".voice-end");
		expect(emitInput).toHaveBeenCalledWith("voice:call", {
			type: "hangup",
			callId: "fixture-0",
			attemptId: sockets[0].attemptId,
		});
		delayed({data: JSON.stringify({type: "ready", engine: "hermes", callId: "fixture-0"})});
		await flushPromises();
		expect(document.querySelector(".voice-call-screen")).toBeNull();
		expect(stop).toHaveBeenCalledOnce();
		wrapper.unmount();
	});
	it("cancels before any ACK and isolates a new attempt from late old frames", async () => {
		const wrapper = render();
		configureRelay(wrapper);
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		const first = sockets[0];
		await click(".voice-end");
		expect(emitInput).toHaveBeenCalledWith("voice:call", {
			type: "hangup",
			attemptId: first.attemptId,
		});
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		const second = sockets[1];
		expect(second.attemptId).toBeGreaterThan(first.attemptId);

		for (const type of ["ready", "audio", "transcript"]) {
			// Deliver on the current listener, not merely the detached old closure.
			relay.listener?.({
				type,
				attemptId: first.attemptId,
				callId: "old",
				engine: "hermes",
				text: "must not send",
				dataUrl: "must not play",
			});
		}

		await flushPromises();
		expect(document.querySelector(".voice-timer")).toBeNull();
		expect(emitInput).not.toHaveBeenCalledWith("input", expect.anything());
		expect(playedSources).toHaveLength(0);
		second.message({type: "ready", engine: "hermes"});
		await flushPromises();
		expect(document.querySelector(".voice-timer")).not.toBeNull();
		wrapper.unmount();
	});
	it("releases a microphone permission request that resolves after End", async () => {
		const wrapper = render();
		configureRelay(wrapper);
		const {promise, resolve} = Promise.withResolvers<MediaStream>();
		getMedia.mockReturnValue(promise);
		await wrapper.get(".call-toggle").trigger("click");
		await click(".voice-end");
		resolve(media as unknown as MediaStream);
		await flushPromises();
		expect(stop).toHaveBeenCalledOnce();
		expect(sockets).toHaveLength(0);
		wrapper.unmount();
	});
	it("preserves the original contact and transcript target during navigation and PiP", async () => {
		const wrapper = render();
		await connect(wrapper);
		await click(".voice-pip");
		await wrapper.setProps({
			channel: {id: 2, name: "#Other", messages: []} as unknown as ClientChan,
		});
		eventbus.emit("voice-call:start", {id: 2, name: "#Other", messages: []});
		await flushPromises();
		expect(document.querySelector(".voice-identity h2")?.textContent).toBe("Gaia");
		expect(sockets).toHaveLength(1);
		sockets[0].message({type: "transcript", text: "hello"});
		expect(emitInput).toHaveBeenCalledWith("input", {target: 1, text: "hello"});
		wrapper.unmount();
	});
	it.each(["channel", "network"])(
		"ends when the actual target %s is removed",
		async (removed) => {
			const wrapper = render();
			state.networks.push({
				uuid: "mirc",
				channels: [wrapper.props("channel")],
			} as ClientNetwork);
			await connect(wrapper);

			if (removed === "channel") {
				state.networks[0].channels.splice(0, 1);
			} else {
				state.networks.splice(0, 1);
			}

			await flushPromises();
			expect(document.querySelector(".voice-call-screen")).toBeNull();
			expect(stop).toHaveBeenCalledOnce();
			expect(sockets[0].close).toHaveBeenCalledOnce();
			expect(sockets[0].send).toHaveBeenCalledWith(JSON.stringify({type: "hangup"}));
			wrapper.unmount();
		}
	);
	it("ends a query call when its registered agent room disappears but the query remains", async () => {
		const wrapper = render();
		const query = {id: 2, name: "Kai", type: "query", messages: []} as unknown as ClientChan;
		const room = {
			id: 1,
			name: "#network_parent/agent",
			type: "channel",
			messages: [],
		} as unknown as ClientChan;
		await wrapper.setProps({channel: query});
		state.networks.push({uuid: "mirc", channels: [room, query]} as ClientNetwork);
		configureRelay(wrapper);
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		sockets[0].message({
			type: "ready",
			engine: "hermes",
			agentName: "Registered Agent",
			agentRoom: room.name,
		});
		await flushPromises();
		sockets[0].message({type: "transcript", text: "query speech"});
		expect(emitInput).toHaveBeenCalledWith("input", {target: 2, text: "query speech"});
		state.networks[0].channels.splice(0, 1);
		await flushPromises();
		expect(state.networks[0].channels).toHaveLength(1);
		expect(document.querySelector(".voice-call-screen")).toBeNull();
		expect(stop).toHaveBeenCalledOnce();
		expect(sockets[0].close).toHaveBeenCalledOnce();
		wrapper.unmount();
	});
	it("can retry the same query after a provider error without treating metadata reset as room removal", async () => {
		const wrapper = render();
		const query = {id: 2, name: "Kai", type: "query", messages: []} as unknown as ClientChan;
		await wrapper.setProps({channel: query});
		state.networks.push({
			uuid: "mirc",
			channels: [{id: 1, name: "#agent", type: "channel", messages: []}, query],
		} as ClientNetwork);
		configureRelay(wrapper);
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		sockets[0].message({
			type: "ready",
			engine: "hermes",
			agentName: "Registered Agent",
			agentRoom: "#agent",
		});
		await flushPromises();
		sockets[0].message({type: "error", message: "Provider failed"});
		await flushPromises();
		await click(".voice-end");
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		expect(sockets).toHaveLength(2);
		sockets[1].message({
			type: "ready",
			engine: "hermes",
			agentName: "Registered Agent",
			agentRoom: "#agent",
		});
		await flushPromises();
		expect(document.querySelector(".voice-timer")).not.toBeNull();
		wrapper.unmount();
	});
	it("ends an established call into visible actionable provider errors", async () => {
		const wrapper = render();
		await connect(wrapper);
		sockets[0].message({
			type: "error",
			message: "STT local assets missing. Run mercury setup stt on the mLounge host.",
		});
		await flushPromises();
		expect(document.querySelector(".voice-timer")).toBeNull();
		expect(document.querySelector(".voice-error")?.textContent).toContain(
			"STT local assets missing"
		);
		expect(document.querySelector(".voice-setup")?.textContent).toContain("mercury setup stt");
		expect(stop).toHaveBeenCalledOnce();
		expect(sockets[0].close).toHaveBeenCalledOnce();
		wrapper.unmount();
	});
	it("surfaces OMP refusal and releases the microphone", async () => {
		const wrapper = render();
		await connect(wrapper);
		sockets[0].message({type: "refused", reason: "OMP voice calls are not supported"});
		await flushPromises();
		expect(document.body.textContent).toContain("OMP voice calls are not supported");
		expect(document.querySelector(".voice-timer")).toBeNull();
		expect(stop).toHaveBeenCalledOnce();
		wrapper.unmount();
	});
	it("switches real outputs directly on Audio taps and preserves completed route state on failure", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{kind: "audiooutput", deviceId: "headset", label: "USB headset"} as MediaDeviceInfo,
			{kind: "audiooutput", deviceId: "speaker", label: "Desk speaker"} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		const {promise, resolve} = Promise.withResolvers<void>();
		sink.mockReturnValueOnce(promise);
		await click(".voice-audio");
		expect(document.querySelector(".voice-audio")?.getAttribute("data-output")).toBe("");
		expect(document.querySelector(".voice-audio.routed")).toBeNull();
		resolve();
		await flushPromises();
		expect(sink).toHaveBeenLastCalledWith("headset");
		expect(document.querySelector(".voice-audio")?.getAttribute("data-output")).toBe("headset");
		expect(document.querySelector(".voice-audio.routed")).not.toBeNull();
		expect(document.querySelector(".voice-audio .voice-route-icon")).not.toBeNull();
		await click(".voice-audio");
		expect(sink).toHaveBeenLastCalledWith("speaker");
		expect(document.querySelector(".voice-audio")?.getAttribute("data-output")).toBe("speaker");
		expect(document.querySelector(".voice-audio.alternate")).not.toBeNull();
		sink.mockRejectedValueOnce(new Error("Permission denied"));
		await click(".voice-audio");
		expect(document.querySelector(".voice-audio")?.getAttribute("data-output")).toBe("speaker");
		expect(document.querySelector(".voice-audio.alternate")).not.toBeNull();
		expect(document.body.textContent).toContain("Output switch failed");
		wrapper.unmount();
	});
	it("preserves distinct non-alias device IDs in one group as separate logical routes", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{
				kind: "audiooutput",
				deviceId: "left",
				groupId: "desk",
				label: "Desk left",
			} as MediaDeviceInfo,
			{
				kind: "audiooutput",
				deviceId: "right",
				groupId: "desk",
				label: "Desk right",
			} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		await click(".voice-audio");
		expect(sink).toHaveBeenLastCalledWith("left");
		await click(".voice-audio");
		expect(sink).toHaveBeenLastCalledWith("right");
		expect(document.querySelector(".voice-audio")?.getAttribute("title")).toBe(
			"Playback: Desk right"
		);
		expect(document.querySelector(".voice-audio.alternate")).not.toBeNull();
		wrapper.unmount();
	});
	it("does not invent alternate routes for default and same-group aliases", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{
				kind: "audiooutput",
				deviceId: "default",
				groupId: "desk",
				label: "Default desk",
			} as MediaDeviceInfo,
			{
				kind: "audiooutput",
				deviceId: "communications",
				groupId: "desk",
				label: "Communications desk",
			} as MediaDeviceInfo,
			{
				kind: "audiooutput",
				deviceId: "desk",
				groupId: "desk",
				label: "Desk speaker",
			} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		await click(".voice-audio");
		expect(sink).not.toHaveBeenCalled();
		expect(document.querySelector(".voice-audio")?.getAttribute("data-output")).toBe("");
		expect(document.body.textContent).toContain("No alternate playback output");
		wrapper.unmount();
	});
	it("switches away from the known default output group and back to its real output", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{
				kind: "audiooutput",
				deviceId: "default",
				groupId: "desk",
				label: "Default desk",
			} as MediaDeviceInfo,
			{
				kind: "audiooutput",
				deviceId: "desk",
				groupId: "desk",
				label: "Desk speaker",
			} as MediaDeviceInfo,
			{
				kind: "audiooutput",
				deviceId: "headset",
				groupId: "usb",
				label: "USB headset",
			} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		await click(".voice-audio");
		expect(sink).toHaveBeenLastCalledWith("headset");
		await click(".voice-audio");
		expect(sink).toHaveBeenLastCalledWith("desk");
		expect(document.querySelector(".voice-audio")?.getAttribute("title")).toBe(
			"Playback: Desk speaker"
		);
		wrapper.unmount();
	});
	it("resets the playback route when a new call creates a default audio element", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{kind: "audiooutput", deviceId: "headset", label: "USB headset"} as MediaDeviceInfo,
			{kind: "audiooutput", deviceId: "desk", label: "Desk speaker"} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		await click(".voice-audio");
		await click(".voice-end");
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		sockets[1].message({type: "ready", engine: "hermes", agentName: "Gaia"});
		await flushPromises();
		expect(document.querySelector(".voice-audio")?.getAttribute("data-output")).toBe("");
		expect(document.querySelector(".voice-audio")?.getAttribute("title")).toBe(
			"Playback: Browser default"
		);
		expect(document.querySelector(".voice-audio.routed")).toBeNull();
		play.mockRejectedValueOnce(new Error("Autoplay blocked"));
		sockets[1].message({
			type: "audio",
			token: "reply-1",
			dataUrl:
				"data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=",
		});
		await flushPromises();
		expect(document.querySelector(".voice-outputs")?.textContent).not.toContain("USB headset");
		expect(document.querySelector(".voice-outputs select")).toBeNull();
		wrapper.unmount();
	});
	it("retains a rejected reply and retries playback synchronously from the Audio gesture", async () => {
		const wrapper = render();
		await connect(wrapper);
		play.mockRejectedValueOnce(new Error("Autoplay blocked"));
		const dataUrl =
			"data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
		sockets[0].message({type: "audio", token: "reply-1", dataUrl});
		await flushPromises();
		expect(document.body.textContent).toContain("Playback was blocked");
		const {promise, resolve} = Promise.withResolvers<MediaDeviceInfo[]>();
		enumerate.mockReturnValue(promise);
		(document.querySelector(".voice-audio") as HTMLButtonElement).click();
		expect(play).toHaveBeenCalledTimes(2);
		resolve([]);
		await flushPromises();
		expect(playedSources).toEqual([dataUrl, dataUrl]);
		wrapper.unmount();
	});
	it("keeps a repeated playback rejection visible instead of overwriting it with routing status", async () => {
		const wrapper = render();
		await connect(wrapper);
		play.mockRejectedValue(new Error("Autoplay still blocked"));
		sockets[0].message({
			type: "audio",
			token: "reply-1",
			dataUrl:
				"data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=",
		});
		await flushPromises();
		await click(".voice-audio");
		expect(document.body.textContent).toContain("Playback was blocked");
		expect(play).toHaveBeenCalledTimes(2);
		wrapper.unmount();
	});
	it("switches the actual playback sink and reports a failed switch without changing selection", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{kind: "audiooutput", deviceId: "headset", label: "USB headset"} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		await click(".voice-audio");
		expect(sink).not.toHaveBeenCalled();
		expect(document.body.textContent).toContain("No alternate playback output");
		const select = document.querySelector("select")!;
		select.value = "headset";
		select.dispatchEvent(new Event("change"));
		await flushPromises();
		expect(sink).toHaveBeenCalledWith("headset");
		expect(document.body.textContent).toContain("Playback: USB headset");
		sink.mockRejectedValueOnce(new Error("Permission denied"));
		select.value = "";
		select.dispatchEvent(new Event("change"));
		await flushPromises();
		expect(select.value).toBe("headset");
		expect(document.body.textContent).toContain("Output switch failed");
		wrapper.unmount();
	});
	it("labels an explicitly selected unnamed output honestly instead of claiming browser default", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{kind: "audiooutput", deviceId: "one", label: ""} as MediaDeviceInfo,
			{kind: "audiooutput", deviceId: "two", label: ""} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		await click(".voice-audio");
		expect(sink).toHaveBeenLastCalledWith("one");
		expect(document.querySelector(".voice-audio")?.getAttribute("title")).toBe(
			"Playback: Audio output 1"
		);
		wrapper.unmount();
	});
	it("honestly reports unsupported browser output selection", async () => {
		const wrapper = render();
		vi.stubGlobal(
			"Audio",
			class extends FakeAudio {
				setSinkId = undefined;
			}
		);
		await connect(wrapper);
		await click(".voice-audio");
		expect(document.querySelector("select")).toBeNull();
		expect(document.body.textContent).toContain("cannot select an audio output");
		wrapper.unmount();
	});
});
