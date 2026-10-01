import { describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { Agent, type AgentToolContext } from "@oh-my-pi/pi-agent-core";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { AsyncJobManager } from "@oh-my-pi/pi-coding-agent/async";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";
import { BashTool } from "@oh-my-pi/pi-coding-agent/tools/bash";

const COMMAND =
	"printf '%s' \"$$\" > steering-pid; printf 'steering-ready\\n'; while [ ! -e steering-release ]; do sleep 0.02; done; printf steering-completed";

function makeSession(cwd: string, manager?: AsyncJobManager): ToolSession {
	return {
		cwd,
		hasUI: false,
		settings: Settings.isolated({
			"async.enabled": false,
			"bash.autoBackground.enabled": false,
			"bashInterceptor.enabled": false,
			"bash.direnv": "off",
		}),
		asyncJobManager: manager,
		getSessionId: () => cwd,
		getSessionFile: () => null,
		getSessionSpawns: () => null,
	};
}

describe("shell steering preserves launched programs", () => {
	for (const managed of [true, false]) {
		it(
			managed
				? "responds while the shell runs and delivers its output later"
				: "lets an untracked shell finish safely",
			async () => {
				const cwd = await fs.mkdtemp(path.join(os.tmpdir(), "mercury-shell-steering-"));
				const deliveries: string[] = [];
				const manager = managed
					? new AsyncJobManager({
							onJobComplete: (_id, text) => {
								deliveries.push(text);
							},
						})
					: undefined;
				const session = makeSession(cwd, manager);
				const settings = session.settings;
				const mock = createMockModel({
					responses: [
						{
							content: [
								{ type: "toolCall", id: "shell", name: "bash", arguments: { command: COMMAND, timeout: 0 } },
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
					if (
						event.type === "tool_execution_update" &&
						JSON.stringify(event.partialResult).includes("steering-ready")
					)
						ready.resolve();
				});
				const watchdog = setTimeout(() => {
					agent.abort();
					ready.reject(new Error("Shell steering did not complete"));
				}, 4_000);
				const run = agent.prompt("start shell work");
				try {
					await ready.promise;
					agent.steer({ role: "user", content: "change direction", attribution: "user", timestamp: Date.now() });
					if (managed) {
						// The release file is deliberately absent: the agent must answer
						// before the command can complete, without terminating its shell.
						await run;
						expect(manager?.getRunningJobs()).toHaveLength(1);
						const results = agent.state.messages.filter(message => message.role === "toolResult");
						expect(results).toHaveLength(1);
						expect(JSON.stringify(results)).toContain("Backgrounded as job");
						expect(JSON.stringify(results)).not.toContain("steering-completed");
					}
					const pid = Number(await Bun.file(path.join(cwd, "steering-pid")).text());
					process.kill(pid, 0);
					await Bun.write(path.join(cwd, "steering-release"), "release");
					await run;
					await manager?.waitForAll();
					expect(mock.calls).toHaveLength(2);
					expect(mock.calls[1].context.messages).toContainEqual(
						expect.objectContaining({ role: "user", content: "change direction" }),
					);
					if (managed) {
						expect(deliveries).toHaveLength(1);
						expect(deliveries[0]).toContain("steering-completed");
						expect(manager?.getAllJobs()[0].status).toBe("completed");
					} else {
						expect(
							JSON.stringify(agent.state.messages.filter(message => message.role === "toolResult")),
						).toContain("steering-completed");
					}
				} finally {
					clearTimeout(watchdog);
					await Bun.write(path.join(cwd, "steering-release"), "release");
					agent.abort();
					await run.catch(() => {});
					await manager?.dispose();
					await fs.rm(cwd, { recursive: true, force: true });
				}
			},
			10_000,
		);
	}

	it("explicit cancellation still stops a tracked foreground command", async () => {
		const cwd = await fs.mkdtemp(path.join(os.tmpdir(), "mercury-shell-cancel-"));
		const manager = new AsyncJobManager({});
		const tool = new BashTool(makeSession(cwd, manager));
		const controller = new AbortController();
		const ready = Promise.withResolvers<void>();
		const watchdog = setTimeout(() => {
			controller.abort();
			ready.reject(new Error("Shell did not start"));
		}, 4_000);
		const call = tool.execute(
			"cancel",
			{ command: COMMAND, timeout: 0 },
			controller.signal,
			update => {
				if (JSON.stringify(update).includes("steering-ready")) ready.resolve();
			},
			{ toolCall: { steeringSignal: new AbortController().signal } } as AgentToolContext,
		);
		try {
			await ready.promise;
			controller.abort();
			await expect(call).rejects.toThrow();
			await manager.waitForAll();
			expect(manager.getAllJobs()[0].status).toBe("cancelled");
			expect(manager.getAllJobs()[0].errorText).not.toContain("steering-completed");
		} finally {
			clearTimeout(watchdog);
			controller.abort();
			await call.catch(() => {});
			await manager.dispose();
			await fs.rm(cwd, { recursive: true, force: true });
		}
	}, 10_000);

	it("keeps foreground shell state and suppresses duplicate completion deliveries", async () => {
		const cwd = await fs.mkdtemp(path.join(os.tmpdir(), "mercury-shell-state-"));
		const deliveries: string[] = [];
		const manager = new AsyncJobManager({
			onJobComplete: (_id, text) => {
				deliveries.push(text);
			},
		});
		const tool = new BashTool(makeSession(cwd, manager));
		const ctx = { toolCall: { steeringSignal: new AbortController().signal } } as AgentToolContext;
		try {
			await tool.execute("set", { command: "export MERCURY_STEERING_STATE=preserved" }, undefined, undefined, ctx);
			const result = await tool.execute(
				"get",
				{ command: "printf '%s' \"$MERCURY_STEERING_STATE\"" },
				undefined,
				undefined,
				ctx,
			);
			await manager.waitForAll();
			expect(JSON.stringify(result.content)).toContain("preserved");
			expect(deliveries).toEqual([]);
		} finally {
			await manager.dispose();
			await fs.rm(cwd, { recursive: true, force: true });
		}
	});
});
