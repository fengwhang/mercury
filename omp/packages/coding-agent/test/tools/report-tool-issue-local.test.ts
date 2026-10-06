import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { pathToFileURL } from "node:url";

const source = path.resolve(import.meta.dir, "../../src");
const moduleUrl = (relative: string): string => pathToFileURL(path.join(source, relative)).href;

async function isolatedRun(script: string, extraEnv: Record<string, string> = {}) {
	const home = fs.mkdtempSync(path.join(os.tmpdir(), "mercury-local-qa-"));
	try {
		const env: NodeJS.ProcessEnv = {
			PATH: process.env.PATH,
			HOME: home,
			MERCURY_HOME: home,
			HERMES_HOME: path.join(home, "hermes"),
			MERCURY_CONFIG: path.join(home, "config.yaml"),
			MERCURY_INHERIT_FROM: path.join(home, "no-inherited-credentials"),
			PI_CONFIG_DIR: ".omp",
			PI_CODING_AGENT_DIR: path.join(home, "agent"),
			XDG_DATA_HOME: path.join(home, "data"),
			XDG_STATE_HOME: path.join(home, "state"),
			XDG_CACHE_HOME: path.join(home, "cache"),
			NO_COLOR: "1",
			...extraEnv,
		};
		const proc = Bun.spawn([process.execPath, "--eval", script], { env, cwd: home, stdout: "pipe", stderr: "pipe" });
		const [stdout, stderr, exitCode] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
		expect(stderr).toBe("");
		expect(exitCode).toBe(0);
		return JSON.parse(stdout);
	} finally {
		fs.rmSync(home, { recursive: true, force: true });
	}
}

const denyNetwork = `let fetchCalls = 0; globalThis.fetch = async () => { fetchCalls++; return new Response("", { status: 200 }); };`;

describe("local report issue entrypoint", () => {
	for (const consent of ["unset", "granted", "denied"]) {
		for (const push of ["0", "1", "absent"]) {
			it(`records locally with legacy consent=${consent} and push=${push}`, async () => {
				const result = await isolatedRun(`${denyNetwork}
					// Fake transport must be installed before these module-loading boundaries.
					const { getAutoQaDbPath } = await import(${JSON.stringify(pathToFileURL(path.resolve(source, "../../utils/src/dirs.ts")).href)});
					if (!getAutoQaDbPath().startsWith(process.env.HOME + "/")) throw new Error("Unisolated QA database");
					const { Settings } = await import(${JSON.stringify(moduleUrl("config/settings.ts"))});
					const issue = await import(${JSON.stringify(moduleUrl("tools/report-tool-issue.ts"))});
					const settings = Settings.isolated(JSON.parse(${JSON.stringify(JSON.stringify({ "dev.autoqaConsent": consent, "dev.autoqaPush.endpoint": "https://qa.invalid/grievances", "dev.autoqaPush.token": "test-token" }))}));
					const response = await issue.dispatchReportIssueDevice({ settings, getActiveModelString: () => "local/test-model" }, "proxy_read: selector lost a line");
					const db = issue.openAutoQaDb();
					const rows = db.prepare("SELECT model, tool, report, created_at FROM grievances").all();
					db.close();
					console.log(JSON.stringify({ rows, fetchCalls, response }));
				`, { ...(push === "absent" ? {} : { PI_AUTO_QA_PUSH: push }), PI_AUTO_QA_PUSH_URL: "https://qa.invalid/override" });
				expect(result.rows).toHaveLength(1);
				expect(result.rows[0]).toMatchObject({ model: "local/test-model", tool: "read", report: "selector lost a line" });
				expect(result.rows[0].created_at).toBeTruthy();
				expect(result.fetchCalls).toBe(0);
				expect(result.response.result.content).toEqual([{ type: "text", text: "Recorded locally." }]);
			});
		}
	}
});

describe("local grievances parser", () => {
	it("rejects the removed push action without invoking fetch", async () => {
		const result = await isolatedRun(`${denyNetwork}
			// Install fake transport before loading the real command/parser.
			const { default: Grievances } = await import(${JSON.stringify(moduleUrl("commands/grievances.ts"))});
			let error;
			try {
				await new Grievances(["push"], { bin: "mercury omp", version: "test", commands: [] }).run();
			} catch (caught) {
				error = { name: caught.name, message: caught.message };
			}
			console.log(JSON.stringify({ error, fetchCalls }));
		`, { PI_AUTO_QA_PUSH: "1", PI_AUTO_QA_PUSH_URL: "https://qa.invalid/override" });
		expect(result.error).toMatchObject({ name: "CliUsageError" });
		expect(result.error.message).toContain('Expected action to be one of: list, clean; got "push"');
		expect(result.fetchCalls).toBe(0);
	});
});

describe("local diagnostic storage", () => {
	it("keeps two-line reports inspectable and cleanable through the real command parser", async () => {
		const result = await isolatedRun(`${denyNetwork}
			// Establish isolated storage before loading recording or CLI modules.
			const { getAutoQaDbPath } = await import(${JSON.stringify(pathToFileURL(path.resolve(source, "../../utils/src/dirs.ts")).href)});
			if (!getAutoQaDbPath().startsWith(process.env.HOME + "/")) throw new Error("Unisolated QA database");
			const { Settings } = await import(${JSON.stringify(moduleUrl("config/settings.ts"))});
			const issue = await import(${JSON.stringify(moduleUrl("tools/report-tool-issue.ts"))});
			const { default: Grievances } = await import(${JSON.stringify(moduleUrl("commands/grievances.ts"))});
			const session = { settings: Settings.isolated() };
			await issue.dispatchReportIssueDevice(session, "grep\\nreported deleted file");
			await issue.dispatchReportIssueDevice(session, "read: lost a line");
			const output = [];
			const log = console.log;
			console.log = value => output.push(JSON.parse(value));
			const config = { bin: "mercury omp", version: "test", commands: [] };
			await new Grievances(["list", "--tool", "grep", "--json"], config).run();
			await new Grievances(["clean", "--tool", "grep", "--json"], config).run();
			await new Grievances(["list", "--json"], config).run();
			console.log = log;
			log(JSON.stringify({ output, fetchCalls }));
		`);
		expect(result.output[0]).toHaveLength(1);
		expect(result.output[0][0]).toMatchObject({ tool: "grep", report: "reported deleted file", model: "unknown" });
		expect(result.output[1]).toEqual({ deleted: 1 });
		expect(result.output[2]).toHaveLength(1);
		expect(result.output[2][0]).toMatchObject({ tool: "read", report: "lost a line" });
		expect(result.fetchCalls).toBe(0);
	});

	it("preserves legacy local reports and timestamps when opening an older database", async () => {
		const result = await isolatedRun(`${denyNetwork}
			// Resolve and check the database path before creating the legacy fixture.
			const { getAutoQaDbPath } = await import(${JSON.stringify(pathToFileURL(path.resolve(source, "../../utils/src/dirs.ts")).href)});
			const dbPath = getAutoQaDbPath();
			if (!dbPath.startsWith(process.env.HOME + "/")) throw new Error("Unisolated QA database");
			const { mkdirSync } = await import("node:fs");
			const { dirname } = await import("node:path");
			const { Database } = await import("bun:sqlite");
			mkdirSync(dirname(dbPath), { recursive: true });
			const legacy = new Database(dbPath);
			legacy.exec("CREATE TABLE grievances (id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, version TEXT NOT NULL, tool TEXT NOT NULL, report TEXT NOT NULL, pushed INTEGER NOT NULL DEFAULT 0)");
			legacy.prepare("INSERT INTO grievances (model, version, tool, report, pushed) VALUES (?, ?, ?, ?, ?)").run("old-model", "old-version", "read", "legacy issue", 1);
			legacy.close();
			const { Settings } = await import(${JSON.stringify(moduleUrl("config/settings.ts"))});
			const issue = await import(${JSON.stringify(moduleUrl("tools/report-tool-issue.ts"))});
			await issue.dispatchReportIssueDevice({ settings: Settings.isolated() }, "grep: new issue");
			const db = issue.openAutoQaDb();
			const rows = db.prepare("SELECT tool, report, created_at FROM grievances ORDER BY id").all();
			db.close();
			console.log(JSON.stringify({ rows, fetchCalls }));
		`);
		expect(result.rows).toHaveLength(2);
		expect(result.rows[0]).toMatchObject({ tool: "read", report: "legacy issue" });
		expect(result.rows[1]).toMatchObject({ tool: "grep", report: "new issue" });
		expect(result.rows.every((row: { created_at: string }) => row.created_at.length > 0)).toBe(true);
		expect(result.fetchCalls).toBe(0);
	});

	it("does not claim recording succeeded when the database cannot be opened", async () => {
		const result = await isolatedRun(`${denyNetwork}
			// Deliberately obstruct only the asserted temporary database path.
			const { getAutoQaDbPath } = await import(${JSON.stringify(pathToFileURL(path.resolve(source, "../../utils/src/dirs.ts")).href)});
			const dbPath = getAutoQaDbPath();
			if (!dbPath.startsWith(process.env.HOME + "/")) throw new Error("Unisolated QA database");
			const { mkdirSync } = await import("node:fs");
			mkdirSync(dbPath, { recursive: true });
			const { Settings } = await import(${JSON.stringify(moduleUrl("config/settings.ts"))});
			const { dispatchReportIssueDevice } = await import(${JSON.stringify(moduleUrl("tools/report-tool-issue.ts"))});
			const response = await dispatchReportIssueDevice({ settings: Settings.isolated() }, "read: lost a line");
			console.log(JSON.stringify({ response, fetchCalls }));
		`);
		expect(result.response.result.content).toEqual([{ type: "text", text: "Could not record issue locally." }]);
		expect(result.fetchCalls).toBe(0);
	});
});
