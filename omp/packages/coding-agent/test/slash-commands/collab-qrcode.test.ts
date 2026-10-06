import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "bun:test";
import { CollabHost } from "@oh-my-pi/pi-coding-agent/collab/host";
import { resetSettingsForTest, Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { initTheme } from "@oh-my-pi/pi-coding-agent/modes/theme/theme";
import type { InteractiveModeContext } from "@oh-my-pi/pi-coding-agent/modes/types";
import {
	type BuiltinSlashCommandRuntime,
	executeBuiltinSlashCommand,
} from "@oh-my-pi/pi-coding-agent/slash-commands/builtin-registry";
import { CollabQrCodeComponent } from "@oh-my-pi/pi-coding-agent/slash-commands/helpers/collab-qrcode";
import { Spacer, Text, visibleWidth } from "@oh-my-pi/pi-tui";

beforeAll(async () => {
	resetSettingsForTest();
	await Settings.init({ inMemory: true });
	await initTheme(false);
});

afterEach(() => {
	vi.restoreAllMocks();
});

afterAll(() => {
	resetSettingsForTest();
});

function fakeHost(options?: {
	webLink?: string;
	webViewLink?: string;
}): NonNullable<InteractiveModeContext["collabHost"]> {
	return {
		link: "relay.example.com/r/full-control",
		viewLink: "relay.example.com/r/read-only",
		webLink: options?.webLink ?? "https://web.example/#full-control",
		webViewLink: options?.webViewLink ?? "https://web.example/#read-only",
		participants: [{ name: "host", role: "host" }],
	} as unknown as NonNullable<InteractiveModeContext["collabHost"]>;
}

function createRuntimeHarness(options?: { collabHost?: NonNullable<InteractiveModeContext["collabHost"]> }) {
	const setText = vi.fn();
	const showStatus = vi.fn();
	const showError = vi.fn();
	const present = vi.fn();
	const settingsGet = vi.fn((key: string) => {
		if (key === "collab.relayUrl") return "wss://relay.example.com";
		if (key === "collab.webUrl") return "";
		return "";
	});
	const ctx = {
		editor: { setText },
		showStatus,
		showError,
		present,
		settings: { get: settingsGet },
		sessionManager: { getSessionId: () => "offline-test", getSessionFile: () => undefined },
		collabHost: options?.collabHost,
	} as unknown as InteractiveModeContext;
	return {
		ctx,
		setText,
		showStatus,
		showError,
		present,
		runtime: { ctx } as BuiltinSlashCommandRuntime,
	};
}

function mockStartedHostLinks() {
	return vi.spyOn(CollabHost.prototype, "start").mockImplementation(function (this: CollabHost): Promise<void> {
		Object.defineProperties(this, {
			link: { value: "relay.example.com/r/full-control", configurable: true },
			viewLink: { value: "relay.example.com/r/read-only", configurable: true },
			webLink: { value: "https://web.example/#started-full", configurable: true },
			webViewLink: { value: "https://web.example/#started-view", configurable: true },
			participants: { value: [{ name: "host", role: "host" as const }], configurable: true },
		});
		return Promise.resolve();
	});
}

describe("collaboration offline entrypoints", () => {
	it("refuses default /collab before constructing a network connection", async () => {
		const network = vi.spyOn(globalThis, "WebSocket").mockImplementation(() => {
			throw new Error("unexpected network");
		});
		const harness = createRuntimeHarness();
		harness.ctx.settings = Settings.instance;
		await executeBuiltinSlashCommand("/collab", harness.runtime);
		expect(harness.showError.mock.calls[0]?.[0]).toContain("collab.relayUrl");
		expect(network).not.toHaveBeenCalled();
	});

	it("refuses a bare /join link before constructing a network connection", async () => {
		const network = vi.spyOn(globalThis, "WebSocket").mockImplementation(() => {
			throw new Error("unexpected network");
		});
		const harness = createRuntimeHarness();
		const key = Buffer.alloc(32, 1).toString("base64url");
		await executeBuiltinSlashCommand(`/join abcdefghijklmnop.${key}`, harness.runtime);
		expect(harness.showError.mock.calls[0]?.[0]).toContain("relay URL");
		expect(network).not.toHaveBeenCalled();
	});

	it("routes an explicit /collab relay to exactly that fake WebSocket endpoint", async () => {
		const network = vi.spyOn(globalThis, "WebSocket").mockImplementation(() => {
			throw new Error("fake relay reached");
		});
		const harness = createRuntimeHarness();
		harness.ctx.settings = Settings.instance;
		await executeBuiltinSlashCommand("/collab relay.example.com:8443", harness.runtime);
		expect(network).toHaveBeenCalledTimes(1);
		expect(network.mock.calls[0]?.[0]).toMatch(/^wss:\/\/relay\.example\.com:8443\/r\/[A-Za-z0-9_-]+\?role=host$/);
		expect(harness.showError.mock.calls[0]?.[0]).toContain("fake relay reached");
	});

	it("routes an explicit /join link to its fake relay without using settings defaults", async () => {
		const network = vi.spyOn(globalThis, "WebSocket").mockImplementation(() => {
			throw new Error("fake relay reached");
		});
		const harness = createRuntimeHarness();
		const key = Buffer.alloc(32, 1).toString("base64url");
		await executeBuiltinSlashCommand(`/join relay.example.com:8443/r/abcdefghijklmnop.${key}`, harness.runtime);
		expect(network).toHaveBeenCalledTimes(1);
		expect(network.mock.calls[0]?.[0]).toBe("wss://relay.example.com:8443/r/abcdefghijklmnop?role=guest");
		expect(harness.showError.mock.calls[0]?.[0]).toContain("fake relay reached");
	});
});

describe("/collab slash command QR code rendering", () => {
	it("starts hosting and prints a one-shot full-control QR", async () => {
		const startSpy = mockStartedHostLinks();
		const harness = createRuntimeHarness();

		const handled = await executeBuiltinSlashCommand("/collab", harness.runtime);

		expect(handled).toBe(true);
		expect(harness.setText).toHaveBeenCalledWith("");
		expect(startSpy).toHaveBeenCalledWith("wss://relay.example.com", "");
		expect(harness.ctx.collabHost).toBeInstanceOf(CollabHost);
		const statusText = harness.showStatus.mock.calls[0]?.[0] as string;
		expect(statusText).toContain("web.example/#started-full");
		const presented = harness.present.mock.calls[0]?.[0] as readonly unknown[];
		expect(presented[0]).toBeInstanceOf(Spacer);
		expect(presented[1]).toBeInstanceOf(CollabQrCodeComponent);
		const component = presented[1] as CollabQrCodeComponent;
		expect(component.url).toBe("https://web.example/#started-full");
		expect(component.render(120).join("\n")).toMatch(/\x1b\[(?:47|40)m/);
	});

	it("starts hosting and prints a one-shot read-only QR", async () => {
		const startSpy = mockStartedHostLinks();
		const harness = createRuntimeHarness();

		const handled = await executeBuiltinSlashCommand("/collab view", harness.runtime);

		expect(handled).toBe(true);
		expect(startSpy).toHaveBeenCalledWith("wss://relay.example.com", "");
		expect(harness.ctx.collabHost).toBeInstanceOf(CollabHost);
		const statusText = harness.showStatus.mock.calls[0]?.[0] as string;
		expect(statusText).toContain("web.example/#started-view");
		expect(statusText).not.toContain("web.example/#started-full");
		const presented = harness.present.mock.calls[0]?.[0] as readonly unknown[];
		expect(presented[0]).toBeInstanceOf(Spacer);
		expect(presented[1]).toBeInstanceOf(CollabQrCodeComponent);
		const component = presented[1] as CollabQrCodeComponent;
		expect(component.url).toBe("https://web.example/#started-view");
	});

	it("prints the active full-control browser QR when hosting", async () => {
		const harness = createRuntimeHarness({ collabHost: fakeHost() });

		const handled = await executeBuiltinSlashCommand("/collab", harness.runtime);

		expect(handled).toBe(true);
		const statusText = harness.showStatus.mock.calls[0]?.[0] as string;
		expect(statusText).toContain("web.example/#full-control");
		const presented = harness.present.mock.calls[0]?.[0] as readonly unknown[];
		expect(presented[0]).toBeInstanceOf(Spacer);
		expect(presented[1]).toBeInstanceOf(CollabQrCodeComponent);
		const component = presented[1] as CollabQrCodeComponent;
		expect(component.render(120).join("\n")).toMatch(/\x1b\[(?:47|40)m/);
	});

	it("prints a one-shot read-only browser QR when hosting", async () => {
		const webLink = "https://web.example/#full-control";
		const webViewLink = "https://web.example/#read-only";
		const harness = createRuntimeHarness({ collabHost: fakeHost({ webLink, webViewLink }) });

		const handled = await executeBuiltinSlashCommand("/collab view", harness.runtime);

		expect(handled).toBe(true);
		const statusText = harness.showStatus.mock.calls[0]?.[0] as string;
		expect(statusText).toContain(webViewLink);
		expect(statusText).not.toContain(webLink);
		const presented = harness.present.mock.calls[0]?.[0] as readonly unknown[];
		expect(presented[0]).toBeInstanceOf(Spacer);
		expect(presented[1]).toBeInstanceOf(CollabQrCodeComponent);
		const component = presented[1] as CollabQrCodeComponent;
		expect(component.url).toBe(webViewLink);
		const clipped = component.render(10).join("\n");
		expect(visibleWidth(clipped)).toBeLessThanOrEqual(10);
		expect(clipped).toContain(webViewLink);
		expect(clipped).not.toContain("URL above");
	});

	it("keeps the browser URL on the first status row so transcript clipping cannot hide it", async () => {
		const webLink = `https://web.example/#${"long-collab-token".repeat(8)}`;
		const harness = createRuntimeHarness({ collabHost: fakeHost({ webLink }) });

		const handled = await executeBuiltinSlashCommand("/collab", harness.runtime);

		expect(handled).toBe(true);
		const statusText = harness.showStatus.mock.calls[0]?.[0] as string;
		const firstRow = new Text(statusText, 1, 0).render(80)[0] ?? "";
		expect(firstRow).toContain("Collab session active");
		expect(firstRow).toContain("Join in browser");
		expect(firstRow).toContain(webLink);
	});
});

describe("CollabQrCodeComponent transcript height clipping", () => {
	it("renders the full half-block symbol when the viewport allocates enough rows", () => {
		const component = new CollabQrCodeComponent("https://web.example/#clip-test");
		const full = component.render(120);
		expect(full.length).toBeGreaterThan(8);
		expect(full.join("\n")).toMatch(/\x1b\[(?:47|40)m/);

		component.setTranscriptAllocation(full.length);
		expect(component.render(120)).toEqual(full);
	});

	it("does not render a quiet-zone white line when the transcript clips to one row", () => {
		const component = new CollabQrCodeComponent("https://web.example/#clip-test");
		const full = component.render(120);
		const first = full[0] ?? "";
		expect(first).toMatch(/\x1b\[(?:47|40)m/);

		component.setTranscriptAllocation(1);
		const clipped = component.render(120);
		expect(clipped).toHaveLength(1);
		expect(clipped[0]).toContain("QR code hidden");
		expect(clipped[0]).toContain("viewport height 1");
		expect(clipped[0]).toContain("web.example/#clip-test");
		expect(clipped[0]).not.toContain("URL above");
		expect(clipped[0]).not.toMatch(/\x1b\[(?:47|40)m/);
	});

	it("keeps the browser URL as the emergency one-row transcript representation", () => {
		const component = new CollabQrCodeComponent("https://web.example/#clip-test");
		component.setTranscriptAllocation(1);
		const row = component.renderTranscriptBlockEmergencyRow(10);
		expect(visibleWidth(row)).toBeLessThanOrEqual(10);
		expect(row).toContain("https://web.example/#clip-test");
		expect(row).not.toContain("URL above");
	});
});
