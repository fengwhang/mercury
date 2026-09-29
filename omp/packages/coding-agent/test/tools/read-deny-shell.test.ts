import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";
import { removeSyncWithRetries } from "@oh-my-pi/pi-utils";
import { BashTool } from "../../src/tools/bash";
import { getShellCredentialReadBlockError } from "../../src/tools/read-deny";

function createApprovalSession(cwd: string): ToolSession {
	return {
		cwd,
		hasUI: false,
		getSessionFile: () => null,
		getSessionSpawns: () => "*",
		settings: Settings.isolated(),
	};
}

/**
 * Shell-layer pair to the read-tool deny: `cat`/`grep`/copy-out of a
 * Mercury credential store must be denied in bash the same way `read`
 * denies it, or the read deny is unpaired theater. Built-in and always
 * on — never user-overridable policy.
 */
describe("shell credential-read guard", () => {
	let mercuryHome: string;
	let prevMercury: string | undefined;
	let prevHermes: string | undefined;

	beforeEach(() => {
		mercuryHome = fs.mkdtempSync(path.join(os.tmpdir(), "pi-read-deny-shell-"));
		prevMercury = process.env.MERCURY_HOME;
		prevHermes = process.env.HERMES_HOME;
		process.env.MERCURY_HOME = mercuryHome;
		delete process.env.HERMES_HOME;
		fs.writeFileSync(path.join(mercuryHome, ".env"), "GITHUB_TOKEN=sentinel\n");
	});

	afterEach(() => {
		if (prevMercury === undefined) delete process.env.MERCURY_HOME;
		else process.env.MERCURY_HOME = prevMercury;
		if (prevHermes === undefined) delete process.env.HERMES_HOME;
		else process.env.HERMES_HOME = prevHermes;
		removeSyncWithRetries(mercuryHome);
	});

	it("blocks reader verbs on the Mercury .env in every home spelling", () => {
		const envFile = path.join(mercuryHome, ".env");
		for (const cmd of [
			`cat ${envFile}`,
			`head -5 ${envFile}`,
			`grep GITHUB_TOKEN ${envFile}`,
			"cat ~/.mercury/.env",
			"cat $HOME/.mercury/.env",
			// oxlint-disable-next-line no-template-curly-in-string -- literal shell command under test
			"cat ${HOME}/.mercury/.env",
			"cat $MERCURY_HOME/.env",
			// oxlint-disable-next-line no-template-curly-in-string -- literal shell command under test
			"cat ${MERCURY_HOME}/.env",
			"cat $HERMES_HOME/.env",
		]) {
			expect(getShellCredentialReadBlockError(cmd), cmd).toBeDefined();
		}
	});

	it("blocks other credential stores and copy-out bypasses", () => {
		for (const cmd of [
			`head -5 ${path.join(mercuryHome, "auth.json")}`,
			`cat ${path.join(mercuryHome, ".anthropic_oauth.json")}`,
			`cp ${path.join(mercuryHome, ".env")} /tmp/x`,
			`tar -cf /tmp/x.tgz ${path.join(mercuryHome, ".env")}`,
			`base64 ${path.join(mercuryHome, ".env")} | head`,
			`cat a && cat ${path.join(mercuryHome, ".env")}`,
			`sh -c 'cat ${path.join(mercuryHome, ".env")}'`,
			`sudo cat ${path.join(mercuryHome, ".env")}`,
			`echo $(cat ${path.join(mercuryHome, ".env")})`,
		]) {
			expect(getShellCredentialReadBlockError(cmd), cmd).toBeDefined();
		}
	});

	it("blocks bare basenames only inside a Mercury cwd", () => {
		expect(getShellCredentialReadBlockError("cat .env", { cwd: mercuryHome })).toBeDefined();
		expect(getShellCredentialReadBlockError("cat auth.json", { cwd: mercuryHome })).toBeDefined();
		expect(getShellCredentialReadBlockError("cat .env", { cwd: os.tmpdir() })).toBeUndefined();
		expect(getShellCredentialReadBlockError("cat .env")).toBeUndefined();
	});

	it("leaves ordinary commands and project .env files alone", () => {
		for (const cmd of [
			"echo hello",
			"ls ~/.mercury/",
			"ls ~/.mercury/.env",
			"echo dont cat ~/.mercury/.env",
			"cat /tmp/proj/.env",
			"cat /tmp/proj/.env.example",
			"npm test",
		]) {
			expect(getShellCredentialReadBlockError(cmd), cmd).toBeUndefined();
		}
	});

	it("denial messages name the path, never secret values", () => {
		const blocked = getShellCredentialReadBlockError(`cat ${path.join(mercuryHome, ".env")}`);
		expect(blocked).toBeDefined();
		expect(blocked).toContain(".env");
		expect(blocked).not.toContain("sentinel");
	});
});

describe("bash tool credential-read approval", () => {
	let mercuryHome: string;
	let prevMercury: string | undefined;
	let prevHermes: string | undefined;

	beforeEach(() => {
		mercuryHome = fs.mkdtempSync(path.join(os.tmpdir(), "pi-read-deny-shell-approval-"));
		prevMercury = process.env.MERCURY_HOME;
		prevHermes = process.env.HERMES_HOME;
		process.env.MERCURY_HOME = mercuryHome;
		delete process.env.HERMES_HOME;
		fs.writeFileSync(path.join(mercuryHome, ".env"), "GITHUB_TOKEN=sentinel\n");
	});

	afterEach(() => {
		if (prevMercury === undefined) delete process.env.MERCURY_HOME;
		else process.env.MERCURY_HOME = prevMercury;
		if (prevHermes === undefined) delete process.env.HERMES_HOME;
		else process.env.HERMES_HOME = prevHermes;
		removeSyncWithRetries(mercuryHome);
	});

	function isDeny(decision: unknown): boolean {
		return (
			typeof decision === "object" &&
			decision !== null &&
			"policy" in decision &&
			decision.policy === "deny"
		);
	}

	it("denies credential reads at approval time, allows ordinary commands", () => {
		const tool = new BashTool(createApprovalSession(os.tmpdir()));
		expect(isDeny(tool.approval({ command: `cat ${path.join(mercuryHome, ".env")}` }))).toBe(true);
		expect(isDeny(tool.approval({ command: "echo hello" }))).toBe(false);
	});
});
