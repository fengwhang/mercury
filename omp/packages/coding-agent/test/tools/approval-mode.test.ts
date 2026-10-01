import { afterAll, beforeAll, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentToolContext } from "@oh-my-pi/pi-agent-core";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { forwardApprovalUI } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import { createSubagentSettings } from "@oh-my-pi/pi-coding-agent/task/executor";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { removeSyncWithRetries, Snowflake } from "@oh-my-pi/pi-utils";

const BASE_SETTINGS = {
	"async.enabled": false,
	"bash.autoBackground.enabled": false,
	"bashInterceptor.enabled": false,
	"bash.patterns": [{ match: "rm -rf *", approval: "deny" }],
} as const;

function emptyWorkspaceTree(cwd: string) {
	return { rootPath: cwd, rendered: ".\n", truncated: false, totalLines: 1, agentsMdFiles: [] };
}

function textOf(result: { content?: ReadonlyArray<{ type: string; text?: string }> }): string {
	const blocks = result.content ?? [];
	for (const block of blocks) {
		if (block.type === "text" && typeof block.text === "string") return block.text;
	}
	return "";
}

describe("tools.approvalMode setting", () => {
	// The per-tool approval gate (ExtensionToolWrapper) reads approvalMode / tools.approval /
	// autoApprove exclusively from the execute-time AgentToolContext, never from the session's
	// own settings. So a single shared session exercises every mode — we only vary the context
	// settings per assertion. This avoids paying createAgentSession's cost (model registry,
	// auth-storage discovery, settings init) nine times over.
	let tempDir: string;
	let session: AgentSession;

	beforeAll(async () => {
		tempDir = fs.mkdtempSync(path.join(os.tmpdir(), `pi-approval-mode-${Snowflake.next()}-`));
		const cwd = path.join(tempDir, "cwd");
		fs.mkdirSync(cwd, { recursive: true });
		const sessionManager = SessionManager.create(cwd, path.join(tempDir, "sessions"));
		const created = await createAgentSession({
			cwd,
			agentDir: tempDir,
			sessionManager,
			settings: Settings.isolated(BASE_SETTINGS),
			model: getBundledModel("openai", "gpt-4o-mini"),
			disableExtensionDiscovery: true,
			skills: [],
			contextFiles: [],
			workspaceTree: emptyWorkspaceTree(cwd),
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			toolNames: ["bash"],
		});
		session = created.session;
	});

	afterAll(async () => {
		await session.dispose();
		// Windows can briefly hold tempdir handles after session.dispose(); retry a few times.
		for (let attempt = 0; attempt < 5; attempt++) {
			try {
				removeSyncWithRetries(tempDir);
				break;
			} catch (err) {
				const code = (err as NodeJS.ErrnoException).code;
				if (code !== "EBUSY" && code !== "ENOTEMPTY" && code !== "EPERM") throw err;
				if (attempt === 4) break; // best-effort: OS will reclaim
				await Bun.sleep(50 * (attempt + 1));
			}
		}
	});

	function approvalSettings(extraSettings: Record<string, unknown> = {}): Settings {
		return Settings.isolated({ ...BASE_SETTINGS, ...extraSettings });
	}

	function bashTool() {
		const bash = session.getToolByName("bash");
		if (!bash) throw new Error("Expected bash tool");
		return bash;
	}

	it("yolo mode bypasses approval for non-overriding tool calls", async () => {
		const settings = approvalSettings({ "tools.approvalMode": "yolo" });
		const result = await bashTool().execute("yolo", { command: "echo ok" }, undefined, undefined, {
			settings,
		} as AgentToolContext);
		expect(textOf(result)).toContain("ok");
	});

	it("always-ask mode rejects exec tools when no UI is available", async () => {
		const settings = approvalSettings({ "tools.approvalMode": "always-ask" });
		await expect(
			bashTool().execute("always-ask", { command: "echo blocked" }, undefined, undefined, {
				settings,
			} as AgentToolContext),
		).rejects.toThrow(/requires approval but no interactive UI available/);
	});

	it("per-tool allow overrides are honored in every mode", async () => {
		const settings = approvalSettings({
			"tools.approvalMode": "always-ask",
			"tools.approval": { bash: "allow" },
		});
		const result = await bashTool().execute("always-ask-allow", { command: "echo allowed" }, undefined, undefined, {
			settings,
		} as AgentToolContext);
		expect(textOf(result)).toContain("allowed");
	});

	it("per-tool prompt overrides can tighten yolo mode", async () => {
		const settings = approvalSettings({
			"tools.approvalMode": "yolo",
			"tools.approval": { bash: "prompt" },
		});
		await expect(
			bashTool().execute("yolo-prompt", { command: "echo blocked" }, undefined, undefined, {
				settings,
			} as AgentToolContext),
		).rejects.toThrow(/requires approval but no interactive UI available/);
	});

	it("write mode still prompts exec-tier tools", async () => {
		const settings = approvalSettings({
			"tools.approvalMode": "write",
			"tools.approval": {},
		});
		await expect(
			bashTool().execute("write-mode", { command: "echo unconfigured" }, undefined, undefined, {
				settings,
			} as AgentToolContext),
		).rejects.toThrow(/requires approval but no interactive UI available/);
	});

	it("critical bash patterns do not prompt in yolo mode with bash allowed", async () => {
		const settings = approvalSettings({
			"tools.approvalMode": "yolo",
			"tools.approval": { bash: "allow" },
		});
		const result = await bashTool().execute(
			"critical",
			{ command: "rm -f /tmp/bun-fake-timer-probe.test.ts" },
			undefined,
			undefined,
			{
				settings,
			} as AgentToolContext,
		);
		expect(textOf(result)).toContain("(no output)");
	});

	it("attributes bash pattern denies to tool policy", async () => {
		const settings = approvalSettings({
			"tools.approvalMode": "yolo",
			"tools.approval": { bash: "allow" },
		});
		await expect(
			bashTool().execute("pattern-deny", { command: "rm -rf /tmp/never-run" }, undefined, undefined, {
				settings,
			} as AgentToolContext),
		).rejects.toThrow('Tool "bash" is blocked by tool policy.\nReason: Blocked by bash pattern: rm -rf *');
	});

	it("CLI --auto-approve forces yolo mode for non-overriding tool calls", async () => {
		const settings = approvalSettings({ "tools.approvalMode": "always-ask" });
		const result = await bashTool().execute("cli-override", { command: "echo override" }, undefined, undefined, {
			settings,
			autoApprove: true,
		} as AgentToolContext);
		expect(textOf(result)).toContain("override");
	});

	it("CLI --auto-approve also bypasses safety-override patterns", async () => {
		const settings = approvalSettings({ "tools.approvalMode": "always-ask" });
		const result = await bashTool().execute(
			"cli-critical",
			{ command: "rm -f /tmp/bun-fake-timer-probe.test.ts" },
			undefined,
			undefined,
			{
				settings,
				autoApprove: true,
			} as AgentToolContext,
		);
		expect(textOf(result)).toContain("(no output)");
	});

	it("xd:// dispatch approval (xdevApproved) suppresses the tier-only re-prompt", async () => {
		// The write tool's outer gate already prompted at the device tool's tier;
		// without the flag this exact call rejects (see the always-ask test above).
		const settings = approvalSettings({ "tools.approvalMode": "always-ask" });
		const result = await bashTool().execute("xdev-tier", { command: "echo dispatched" }, undefined, undefined, {
			settings,
			xdevApproved: true,
		} as AgentToolContext);
		expect(textOf(result)).toContain("dispatched");
	});

	it("xdevApproved does not bypass explicit per-tool prompt or deny policies", async () => {
		const promptSettings = approvalSettings({
			"tools.approvalMode": "always-ask",
			"tools.approval": { bash: "prompt" },
		});
		await expect(
			bashTool().execute("xdev-explicit-prompt", { command: "echo blocked" }, undefined, undefined, {
				settings: promptSettings,
				xdevApproved: true,
			} as AgentToolContext),
		).rejects.toThrow(/requires approval but no interactive UI available/);

		const denySettings = approvalSettings({
			"tools.approvalMode": "always-ask",
			"tools.approval": { bash: "deny" },
		});
		await expect(
			bashTool().execute("xdev-denied", { command: "echo blocked" }, undefined, undefined, {
				settings: denySettings,
				xdevApproved: true,
			} as AgentToolContext),
		).rejects.toThrow(/blocked by user policy/);
	});

	it("constructs an extensionRunner unconditionally so the approval gate is always installed", async () => {
		// Regression lock for the architectural fix: the per-tool approval gate is implemented
		// inside `ExtensionToolWrapper`, which is only attached when `session.extensionRunner` exists.
		// Historically the runner was conditional on `extensionsResult.extensions.length > 0`, which
		// meant the entire approval system silently disappeared for users with no extensions loaded —
		// any non-yolo approval mode setting would be a no-op without feedback. The
		// fix is to construct the runner unconditionally; this test makes that contract explicit so
		// a future change to make the runner optional again cannot silently re-open the hole.
		expect(session.extensionRunner).toBeDefined();
	});
	it("grandchild approval reaches the orchestrator and follows live family policy", async () => {
		const parentSettings = approvalSettings({ "tools.approvalMode": "always-ask" });
		const childSettings = createSubagentSettings(parentSettings, { "tools.approvalMode": "yolo" });
		const grandchildSettings = createSubagentSettings(childSettings);
		const originalUI = session.extensionRunner!.getUIContext();
		const titles: string[] = [];
		let answer = "Approve";
		const parentUI = {
			...originalUI,
			select: async (title: string) => {
				titles.push(title);
				return answer;
			},
		};
		await initializeExtensions(session, {
			uiContext: forwardApprovalUI(forwardApprovalUI(parentUI, "child"), "grandchild"),
			reportSendError: () => {},
			reportRuntimeError: () => {},
		});
		try {
			const result = await bashTool().execute(
				"grandchild-approved",
				{ command: "echo approved-by-parent" },
				undefined,
				undefined,
				{ settings: grandchildSettings } as AgentToolContext,
			);
			expect(textOf(result)).toContain("approved-by-parent");
			expect(titles).toHaveLength(1);
			expect(titles[0]).toContain("[child] [grandchild]");
			answer = "Deny";
			await expect(
				bashTool().execute("grandchild-denied", { command: "echo denied" }, undefined, undefined, {
					settings: grandchildSettings,
				} as AgentToolContext),
			).rejects.toThrow(/denied/);
			parentSettings.override("tools.approvalMode", "yolo");
			await bashTool().execute("grandchild-yolo", { command: "echo inherited-yolo" }, undefined, undefined, {
				settings: grandchildSettings,
			} as AgentToolContext);
			expect(titles).toHaveLength(2);
			parentSettings.override("tools.approval", { bash: "deny" });
			await expect(
				bashTool().execute("grandchild-policy-deny", { command: "echo denied-in-yolo" }, undefined, undefined, {
					settings: grandchildSettings,
				} as AgentToolContext),
			).rejects.toThrow(/blocked/);
			expect(titles).toHaveLength(2);
		} finally {
			await initializeExtensions(session, {
				uiContext: originalUI,
				reportSendError: () => {},
				reportRuntimeError: () => {},
			});
		}
	});
	it("a live Mercury policy controls grandchild commands without restarting the room", async () => {
		const profile = path.join(tempDir, "mercury-live-policy.yaml");
		const writePolicy = (text: string) => fs.writeFileSync(profile, text);
		writePolicy(
			'approvals: {mode: yolo}\nomp:\n  # Mercury inherited deny patterns: [{"match":"*ECHO DENIED*","approval":"deny"}]\n  tools: {approvalMode: yolo}\n',
		);
		const root = Settings.isolated({
			...BASE_SETTINGS,
			"tools.approvalMode": "yolo",
			"bash.patterns": [
				{ match: "echo *", approval: "allow" },
				{ match: "*ECHO DENIED*", approval: "deny" },
			],
		}).useMercuryApprovalPolicy(profile);
		const family = createSubagentSettings(createSubagentSettings(root));
		const created = await createAgentSession({
			cwd: tempDir,
			agentDir: tempDir,
			settings: family,
			model: getBundledModel("openai", "gpt-4o-mini"),
			disableExtensionDiscovery: true,
			skills: [],
			contextFiles: [],
			workspaceTree: emptyWorkspaceTree(tempDir),
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			enableMirc: false,
			toolNames: ["bash"],
		});
		let prompts = 0;
		const originalUI = created.session.extensionRunner!.getUIContext();
		await initializeExtensions(created.session, {
			uiContext: forwardApprovalUI(
				{
					...originalUI,
					select: async () => {
						prompts++;
						return "Approve";
					},
				},
				"grandchild",
			),
			reportSendError: () => {},
			reportRuntimeError: () => {},
		});
		const tool = created.session.getToolByName("bash")!;
		const execute = (command: string) =>
			tool.execute("live-policy", { command }, undefined, undefined, { settings: family } as AgentToolContext);
		try {
			expect(textOf(await execute("printf live-yolo"))).toContain("live-yolo");
			expect(prompts).toBe(0);
			writePolicy("approvals: {mode: safe}\n");
			expect(textOf(await execute("printf live-safe"))).toContain("live-safe");
			expect(prompts).toBe(1);
			writePolicy("approvals: {mode: smart}\n");
			await execute("printf live-smart");
			expect(prompts).toBe(2);
			writePolicy('approvals: {mode: yolo, deny: ["*ECHO DENIED*"]}\n');
			await expect(execute("echo denied")).rejects.toThrow(/Blocked by bash pattern/);
			expect(prompts).toBe(2);
			writePolicy("approvals: {mode: yolo, deny: []}\n");
			expect(textOf(await execute("echo denied"))).toContain("denied");
			writePolicy("approvals: {mode: yolo, deny: invalid}\n");
			await expect(execute("printf invalid-policy-must-not-run")).rejects.toThrow(
				/Cannot enforce Mercury approval policy/,
			);
		} finally {
			await created.session.dispose();
		}
	});
});
