import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";
import { removeSyncWithRetries } from "@oh-my-pi/pi-utils";
import { GrepTool } from "../../src/tools/grep";
import { ReadTool } from "../../src/tools/read";
import { getReadBlockError } from "../../src/tools/read-deny";

const SENTINEL_PASSWORD = "obs-pw-SENTINEL-7c3a9e1f5b2d";
const SENTINEL_TOKEN = "syt-SENTINEL-4f8c2a6e0b1d3f5a";

const testSettings = Settings.isolated();

function createTestSession(cwd: string): ToolSession {
	return {
		cwd,
		hasUI: false,
		getSessionFile: () => null,
		getSessionSpawns: () => "*",
		settings: testSettings,
	};
}

function getText(result: { content: Array<{ type: string; text?: string }> }): string {
	return result.content
		.filter(entry => entry.type === "text")
		.map(entry => entry.text ?? "")
		.join("\n");
}

/** Plant a fake $MERCURY_HOME/observatory tree holding sentinel secrets. */
function plantObservatory(home: string): { creds: string; toml: string; reg: string } {
	const obs = path.join(home, "observatory");
	fs.mkdirSync(path.join(obs, "appservices"), { recursive: true });
	fs.mkdirSync(path.join(obs, "logs"), { recursive: true });
	const creds = path.join(obs, "owner-credentials.json");
	fs.writeFileSync(
		creds,
		JSON.stringify({
			homeserver_url: "http://127.0.0.1:18008",
			user_id: "@merc-owner:mercury.local",
			password: SENTINEL_PASSWORD,
			access_token: SENTINEL_TOKEN,
			device_id: "DEV1",
		}),
	);
	const toml = path.join(obs, "tuwunel.toml");
	fs.writeFileSync(toml, `[global]\nregistration_token = "reg-${SENTINEL_PASSWORD}"\n`);
	const reg = path.join(obs, "appservices", "merc-observatory.yaml");
	fs.writeFileSync(reg, `as_token: "as-${SENTINEL_TOKEN}"\nhs_token: "hs-${SENTINEL_TOKEN}"\n`);
	fs.writeFileSync(path.join(obs, "logs", "homeserver.log"), "server started\n");
	return { creds, toml, reg };
}

describe("read-deny observatory secrets", () => {
	let mercuryHome: string;
	let prevMercury: string | undefined;
	let prevHermes: string | undefined;
	let paths: { creds: string; toml: string; reg: string };

	beforeEach(() => {
		mercuryHome = fs.mkdtempSync(path.join(os.tmpdir(), "pi-read-deny-owner-"));
		prevMercury = process.env.MERCURY_HOME;
		prevHermes = process.env.HERMES_HOME;
		process.env.MERCURY_HOME = mercuryHome;
		delete process.env.HERMES_HOME;
		paths = plantObservatory(mercuryHome);
	});

	afterEach(() => {
		if (prevMercury === undefined) delete process.env.MERCURY_HOME;
		else process.env.MERCURY_HOME = prevMercury;
		if (prevHermes === undefined) delete process.env.HERMES_HOME;
		else process.env.HERMES_HOME = prevHermes;
		removeSyncWithRetries(mercuryHome);
	});

	it("blocks every observatory plaintext-secret file", () => {
		for (const file of [paths.creds, paths.toml, paths.reg]) {
			expect(getReadBlockError(file)).toBeDefined();
		}
	});

	it("leaves non-secret observatory files readable", () => {
		expect(getReadBlockError(path.join(mercuryHome, "observatory", "logs", "homeserver.log"))).toBeUndefined();
	});

	it("widens to the Mercury root in profile mode", () => {
		const profileHome = path.join(mercuryHome, "profiles", "coder");
		fs.mkdirSync(profileHome, { recursive: true });
		process.env.MERCURY_HOME = profileHome;
		expect(getReadBlockError(paths.creds)).toBeDefined();
		expect(getReadBlockError(path.join(profileHome, "observatory", "owner-credentials.json"))).toBeDefined();
	});

	it("denial messages are location-only, never secret values", () => {
		const blocked = getReadBlockError(paths.creds);
		expect(blocked).toBeDefined();
		expect(blocked).toContain("owner-credentials.json");
		expect(blocked).not.toContain(SENTINEL_PASSWORD);
		expect(blocked).not.toContain(SENTINEL_TOKEN);
	});
});

describe("read tool observatory denylist", () => {
	let mercuryHome: string;
	let prevMercury: string | undefined;
	let prevHermes: string | undefined;
	let paths: { creds: string; toml: string; reg: string };

	beforeEach(() => {
		mercuryHome = fs.mkdtempSync(path.join(os.tmpdir(), "pi-read-owner-"));
		prevMercury = process.env.MERCURY_HOME;
		prevHermes = process.env.HERMES_HOME;
		process.env.MERCURY_HOME = mercuryHome;
		delete process.env.HERMES_HOME;
		paths = plantObservatory(mercuryHome);
	});

	afterEach(() => {
		if (prevMercury === undefined) delete process.env.MERCURY_HOME;
		else process.env.MERCURY_HOME = prevMercury;
		if (prevHermes === undefined) delete process.env.HERMES_HOME;
		else process.env.HERMES_HOME = prevHermes;
		removeSyncWithRetries(mercuryHome);
	});

	it("refuses owner-credentials.json reads without leaking values", async () => {
		const tool = new ReadTool(createTestSession(mercuryHome));
		const failure = await tool.execute("read-creds", { path: paths.creds }).then(
			() => null,
			(error: unknown) => String(error),
		);
		expect(failure).not.toBeNull();
		expect(failure).toMatch(/Access denied/i);
		expect(failure).not.toContain(SENTINEL_PASSWORD);
		expect(failure).not.toContain(SENTINEL_TOKEN);
	});

	it("still reads non-secret observatory files", async () => {
		const tool = new ReadTool(createTestSession(mercuryHome));
		const log = await tool.execute("read-log", {
			path: path.join(mercuryHome, "observatory", "logs", "homeserver.log"),
		});
		expect(getText(log)).toContain("server started");
	});
});

describe("grep tool observatory denylist", () => {
	let mercuryHome: string;
	let prevMercury: string | undefined;
	let prevHermes: string | undefined;

	beforeEach(() => {
		mercuryHome = fs.mkdtempSync(path.join(os.tmpdir(), "pi-grep-owner-"));
		prevMercury = process.env.MERCURY_HOME;
		prevHermes = process.env.HERMES_HOME;
		process.env.MERCURY_HOME = mercuryHome;
		delete process.env.HERMES_HOME;
		plantObservatory(mercuryHome);
	});

	afterEach(() => {
		if (prevMercury === undefined) delete process.env.MERCURY_HOME;
		else process.env.MERCURY_HOME = prevMercury;
		if (prevHermes === undefined) delete process.env.HERMES_HOME;
		else process.env.HERMES_HOME = prevHermes;
		removeSyncWithRetries(mercuryHome);
	});

	it("omits observatory secret match content without leaking values", async () => {
		const tool = new GrepTool(createTestSession(mercuryHome));
		const result = await tool.execute("grep-owner", {
			pattern: "SENTINEL",
			path: path.join(mercuryHome, "observatory"),
		});
		const text = getText(result);
		expect(text).not.toContain(SENTINEL_PASSWORD);
		expect(text).not.toContain(SENTINEL_TOKEN);
		expect(text).toMatch(/secret-bearing|Omitted matches/i);
	});
});
