// @vitest-environment jsdom
import {afterEach, beforeEach, describe, expect, it, vi} from "vitest";
import {flushPromises, mount, VueWrapper} from "@vue/test-utils";
import VoiceCall from "../../../client/components/VoiceCall.vue";
import type {ClientChan} from "../../../client/js/types";
import eventbus from "../../../client/js/eventbus";

const state = vi.hoisted(() => ({
	isConnected: true,
	serverConfiguration: {voiceCallSidecarUrl: ""},
}));
const recordings = vi.hoisted(() => [] as Array<(blob: Blob) => void>);
const emitInput = vi.hoisted(() => vi.fn());
vi.mock("../../../client/js/store", () => ({useStore: () => ({state})}));
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
class FakeAudio {
	src = "";
	onended: (() => void) | null = null;
	onerror: (() => void) | null = null;
	setSinkId: ((id: string) => Promise<void>) | undefined = sink;
	play = vi.fn().mockResolvedValue(undefined);
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
	recordings.length = 0;
	sink.mockClear();
	pause.mockClear();
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
	it("switches the actual playback sink and reports a failed switch without changing selection", async () => {
		const wrapper = render();
		enumerate.mockResolvedValue([
			{kind: "audiooutput", deviceId: "headset", label: "USB headset"} as MediaDeviceInfo,
		]);
		await connect(wrapper);
		await click(".voice-audio");
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
