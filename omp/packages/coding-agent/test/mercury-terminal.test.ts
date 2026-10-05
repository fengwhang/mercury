import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { executeBash } from "../src/exec/bash-executor";
import { closeMercuryTerminals, mercuryTerminalBackend } from "../src/exec/mercury-terminal";
import { BashTool } from "../src/tools/bash";
import type { ToolSession } from "../src/tools";
import { Settings } from "../src/config/settings";
import { workerEnvFromParent } from "../src/subprocess/worker-client";
import { Agent, type AgentToolContext } from "@oh-my-pi/pi-agent-core";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { AsyncJobManager } from "../src/async";
import { BashRunner } from "../src/session/bash-runner";
import { SessionManager } from "../src/session/session-manager";

const folders: string[] = [];
let restoreEnv: (() => void) | undefined;
afterEach(() => {
	closeMercuryTerminals();
	restoreEnv?.();
	restoreEnv = undefined;
	for (const folder of folders.splice(0)) fs.rmSync(folder, { recursive: true, force: true });
});

function temp(): string {
	const folder = fs.mkdtempSync(path.join(os.tmpdir(), "mercury-terminal-test-"));
	folders.push(folder);
	return folder;
}

describe("Mercury terminal selection", () => {
	it("explicit current config overrides stale environment and local selection clears SSH", () => {
		const home = temp();
		const config = path.join(home, "config.yaml");
		const env = { MERCURY_HOME: home, MERCURY_CONFIG: config, TERMINAL_ENV: "local" };
		fs.writeFileSync(config, "hermes:\n  terminal:\n    backend: ssh\n");
		expect(mercuryTerminalBackend(env)).toBe("ssh");
		fs.writeFileSync(config, "hermes:\n  terminal:\n    backend: local\n");
		expect(mercuryTerminalBackend({ ...env, TERMINAL_ENV: "ssh" })).toBe("local");
		fs.writeFileSync(config, "hermes: {}\n");
		expect(mercuryTerminalBackend({ ...env, TERMINAL_ENV: "docker" })).toBe("docker");
	});
	it("refuses malformed config rather than executing locally", () => {
		const home = temp();
		const config = path.join(home, "config.yaml");
		fs.writeFileSync(config, "hermes:\n  terminal: []\n");
		expect(() => mercuryTerminalBackend({ MERCURY_CONFIG: config })).toThrow("must be a mapping");
		fs.writeFileSync(config, "hermes: [\n");
		expect(() => mercuryTerminalBackend({ MERCURY_CONFIG: config })).toThrow();
	});
});

const sshFixture = process.env.MERCURY_TEST_SSH_FIXTURE;
describe.skipIf(!sshFixture)("Mercury real SSH shell", () => {
	function configure(): string {
		const ssh = JSON.parse(fs.readFileSync(sshFixture!, "utf8")) as { port: number; key: string };
		const home = temp();
		const config = path.join(home, "config.yaml");
		fs.writeFileSync(
			config,
			`hermes:\n  terminal:\n    backend: ssh\n    ssh_host: 127.0.0.1\n    ssh_user: user\n    ssh_port: ${ssh.port}\n    ssh_key: ${JSON.stringify(ssh.key)}\n    cwd: "~"\nomp:\n  tools:\n    approvalMode: yolo\n`,
		);
		const overlay = {
			HOME: home,
			MERCURY_HOME: home,
			HERMES_HOME: path.join(home, "hermes"),
			MERCURY_CONFIG: config,
			MERCURY_REPO: path.resolve(import.meta.dir, "../../../.."),
			MERCURY_PYTHON: process.env.MERCURY_TEST_PYTHON || "/tmp/mercury-review-venv/bin/python",
			TERMINAL_ENV: "local",
			TERMINAL_SSH_HOST: "obsolete.invalid",
		};
		const previous = Object.fromEntries(Object.keys(overlay).map(key => [key, process.env[key]]));
		Object.assign(process.env, overlay);
		restoreEnv = () => {
			for (const [key, value] of Object.entries(previous)) {
				if (value === undefined) delete process.env[key];
				else process.env[key] = value;
			}
		};
		return home;
	}
	it("runs remotely, preserves cwd, quotes overlays, and propagates through child and grandchild processes", async () => {
		const home = configure();
		const first = await executeBash("test ! -e /nix/store; mkdir -p project; cd project; pwd", {
			cwd: "",
			sessionKey: "parent",
			timeout: 20_000,
		});
		expect(first.exitCode).toBe(0);
		expect(first.output.trim()).toBe("/home/user/project");
		const second = await executeBash("printf '%s' \"$CHECK_VALUE\"; pwd", {
			cwd: "",
			sessionKey: "parent",
			env: { CHECK_VALUE: "dollars$ apostrophe' spaces_! " },
			timeout: 0,
		});
		expect(second.output).toContain("dollars$ apostrophe' spaces_! /home/user/project");
		const child = path.join(home, "child.ts");
		fs.writeFileSync(
			child,
			`import {executeBash} from ${JSON.stringify(path.resolve(import.meta.dir, "../src/exec/bash-executor.ts"))};\nimport {closeMercuryTerminals} from ${JSON.stringify(path.resolve(import.meta.dir, "../src/exec/mercury-terminal.ts"))};\nimport {workerEnvFromParent} from ${JSON.stringify(path.resolve(import.meta.dir, "../src/subprocess/worker-client.ts"))};\nif (!process.argv.includes("grandchild")) { const child=Bun.spawn([process.execPath,import.meta.path,"grandchild"],{env:workerEnvFromParent(),stdout:"pipe",stderr:"pipe"});const output=await new Response(child.stdout).text();if(await child.exited!==0) throw new Error("grandchild failed");process.stdout.write(output); } else { const r=await executeBash("test ! -e /nix/store; echo grandchild-remote", {cwd:"",timeout:20000});console.log(JSON.stringify(r)); } closeMercuryTerminals();`,
		);
		const processChild = Bun.spawn([process.execPath, child], {
			env: workerEnvFromParent(),
			stdout: "pipe",
			stderr: "pipe",
		});
		const output = await new Response(processChild.stdout).text();
		expect(await processChild.exited).toBe(0);
		expect(JSON.parse(output).output).toContain("grandchild-remote");
	}, 60_000);
	it("TUI shell commands start remotely and closing one session preserves another", async () => {
		configure();
		const manager = SessionManager.inMemory("/host-only-missing-directory");
		const mock = createMockModel({ responses: [{ content: ["ok"] }] });
		const agent = new Agent({ initialState: { model: mock.model }, streamFn: mock.stream });
		const runner = new BashRunner({
			agent,
			sessionManager: manager,
			settings: Settings.isolated({}),
			extensionRunner: () => undefined,
			isStreaming: () => false,
		});
		try {
			const result = await runner.executeBash("test ! -e /nix/store; pwd");
			expect(result.exitCode).toBe(0);
			expect(result.output.trim()).toBe("/home/user");
			await executeBash("mkdir -p survivor; cd survivor", { cwd: "", sessionKey: "survivor" });
			closeMercuryTerminals(manager.getSessionId());
			const surviving = await executeBash("pwd", { cwd: "", sessionKey: "survivor" });
			expect(surviving.output.trim()).toBe("/home/user/survivor");
		} finally {
			await manager.close();
		}
	}, 60_000);
	it("steering answers while the remote program keeps running and delivers its result", async () => {
		configure();
		const remote = `/home/user/steering-${crypto.randomUUID()}`;
		const deliveries: string[] = [];
		const manager = new AsyncJobManager({
			onJobComplete: (_id, text) => {
				deliveries.push(text);
			},
		});
		const settings = Settings.isolated({
			"async.enabled": false,
			"bash.autoBackground.enabled": false,
			"bashInterceptor.enabled": false,
		});
		const session = {
			settings,
			cwd: process.cwd(),
			asyncJobManager: manager,
			getSessionId: () => remote,
			getSessionFile: () => null,
			getSessionSpawns: () => null,
		} as ToolSession;
		const mock = createMockModel({
			responses: [
				{
					content: [
						{
							type: "toolCall",
							id: "shell",
							name: "bash",
							arguments: {
								command: `mkdir -p '${remote}'; echo steering-ready; while [ ! -e '${remote}/release' ]; do sleep 0.02; done; echo steering-completed`,
								timeout: 0,
							},
						},
					],
					stopReason: "toolUse",
				},
				{ content: ["changed direction"] },
			],
		});
		const agent = new Agent({
			initialState: { model: mock.model, tools: [new BashTool(session)] },
			streamFn: mock.stream,
			getToolContext: toolCall => ({ settings, toolCall }) as AgentToolContext,
		});
		const ready = Promise.withResolvers<void>();
		agent.subscribe(event => {
			if (event.type === "tool_execution_update" && JSON.stringify(event.partialResult).includes("steering-ready"))
				ready.resolve();
		});
		const watchdog = setTimeout(() => {
			agent.abort();
			ready.reject(new Error("Remote streaming/steering stalled"));
		}, 12_000);
		const run = agent.prompt("start remote work");
		try {
			await ready.promise;
			agent.steer({ role: "user", content: "change direction", attribution: "user", timestamp: Date.now() });
			await run;
			expect(manager.getRunningJobs()).toHaveLength(1);
			expect(mock.calls).toHaveLength(2);
			await executeBash(`touch '${remote}/release'`, { sessionKey: "release", timeout: 20_000 });
			await manager.waitForAll();
			expect(deliveries).toHaveLength(1);
			expect(deliveries[0]).toContain("steering-completed");
		} finally {
			clearTimeout(watchdog);
			await executeBash(`touch '${remote}/release'`, { sessionKey: "release", timeout: 20_000 }).catch(() => {});
			await run.catch(() => {});
			await manager.dispose();
		}
	}, 40_000);
	it("accepts a remote-only cwd and does not route PTY or client terminals locally", async () => {
		configure();
		const settings = Settings.isolated({ "async.enabled": false, "bashInterceptor.enabled": false });
		const session = {
			settings,
			cwd: process.cwd(),
			getSessionId: () => "tool",
			getSessionFile: () => null,
			getSessionSpawns: () => null,
			getClientBridge: () => ({
				capabilities: { terminal: true },
				createTerminal: () => {
					throw new Error("Local client terminal ran");
				},
			}),
		} as unknown as ToolSession;
		const tool = new BashTool(session);
		const result = await tool.execute(
			"remote-cwd",
			{ command: "pwd", cwd: "/home/user/project", pty: true },
			undefined,
		);
		expect(result.content).toContainEqual(
			expect.objectContaining({ type: "text", text: expect.stringContaining("/home/user/project") }),
		);
	}, 60_000);
	it("a failed SSH connection cannot execute a command on the host", async () => {
		const home = configure();
		const config = path.join(home, "config.yaml");
		const text = fs.readFileSync(config, "utf8").replace(/ssh_port: \d+/, "ssh_port: 1");
		fs.writeFileSync(config, text);
		const marker = path.join(home, "should-not-exist");
		await expect(executeBash(`touch '${marker}'`, { sessionKey: "failed", timeout: 1000 })).rejects.toThrow();
		expect(fs.existsSync(marker)).toBe(false);
	}, 30_000);
});
