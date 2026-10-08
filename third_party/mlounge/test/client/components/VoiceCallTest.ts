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
vi.mock("../../../client/js/socket", () => ({default: {emit: emitInput}}));

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
	constructor() {
		FakeSocket.instances.push(this);
	}
	message(value: object) {
		this.onmessage?.({data: JSON.stringify(value)});
	}
}
// Only FakeSocket constructors register instances.
const sockets = FakeSocket.instances as FakeSocket[];
const getMedia = vi.fn();
const enumerate = vi.fn();
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
	state.serverConfiguration.voiceCallSidecarUrl = "https://voice.example.test";
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
		state.serverConfiguration.voiceCallSidecarUrl = "https://voice.example.test";
		await wrapper.get(".call-toggle").trigger("click");
		await flushPromises();
		sockets[0].message({type: "ready", engine: "hermes", agentName: "Registered Agent"});
		await flushPromises();
		expect(document.querySelector(".voice-identity h2")?.textContent).toBe("Registered Agent");
		wrapper.unmount();
	});
	it("opens actionable settings outside the clipping chat header when unconfigured", async () => {
		const wrapper = render();
		await wrapper.get(".call-toggle").trigger("click");
		expect(document.body.querySelector(".voice-settings input")).not.toBeNull();
		expect(wrapper.element.contains(document.body.querySelector(".voice-settings"))).toBe(
			false
		);
		expect(document.body.textContent).toContain("sidecar URL");
		wrapper.unmount();
	});
	it("starts from the configured URL and starts the timer only at ready", async () => {
		const wrapper = render();
		state.serverConfiguration.voiceCallSidecarUrl = "https://voice.example.test";
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
	it("releases a microphone permission request that resolves after End", async () => {
		const wrapper = render();
		state.serverConfiguration.voiceCallSidecarUrl = "https://voice.example.test";
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
		state.serverConfiguration.voiceCallSidecarUrl = "https://voice.example.test";
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
		state.serverConfiguration.voiceCallSidecarUrl = "https://voice.example.test";
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
	it("ends an established call into visible actionable provider error settings", async () => {
		const wrapper = render();
		await connect(wrapper);
		sockets[0].message({
			type: "error",
			message: "STT local assets missing. Configure STT on the mLounge host.",
		});
		await flushPromises();
		expect(document.querySelector(".voice-timer")).toBeNull();
		expect(document.querySelector(".voice-error")?.textContent).toContain(
			"STT local assets missing"
		);
		expect(document.querySelector(".voice-setup")?.textContent).toContain("Voice settings");
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
