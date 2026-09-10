import { describe, expect, it, vi } from "bun:test";
import {
	dispatchRpcSubagentControl,
	type RpcSubagentControlCommand,
} from "@oh-my-pi/pi-coding-agent/modes/rpc/rpc-mode";
import type { RpcResponse } from "@oh-my-pi/pi-coding-agent/modes/rpc/rpc-types";
import type { AgentLifecycleManager } from "@oh-my-pi/pi-coding-agent/registry/agent-lifecycle";
import type { AgentRef } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import { USER_INTERRUPT_LABEL } from "@oh-my-pi/pi-coding-agent/session/messages";

// Contract (HERMES-OMP PATCH, matrix observatory §8.2): `subagent_steer` /
// `subagent_abort` drive an in-process subagent through the lifecycle manager —
// ensureLive + prompt({ streamingBehavior: "steer" }) for steering (collab host
// agent "chat"), session.abort + release({ tombstone: true }) for kills (Agent
// Hub kill). Unknown, advisor, and main-session ids are refused with distinct
// machine-readable codes; a parked isolated ref (no reviver) reports that
// clearly instead of a raw revive exception.

interface HarnessOptions {
	kind?: AgentRef["kind"];
	status?: AgentRef["status"];
	withSession?: boolean;
	ensureLiveError?: Error;
	releaseError?: Error;
}

function createHarness(options: HarnessOptions = {}) {
	const prompt = vi.fn(async () => true);
	const abort = vi.fn(async () => {});
	const session = { prompt, abort };
	const ref: AgentRef = {
		id: "Worker",
		displayName: "Worker",
		kind: options.kind ?? "sub",
		parentId: "Main",
		status: options.status ?? "running",
		session: options.withSession === false ? null : (session as unknown as AgentRef["session"]),
		sessionFile: "/tmp/worker.jsonl",
		createdAt: Date.now(),
		lastActivity: Date.now(),
	};
	const ensureLive = vi.fn(async () => {
		if (options.ensureLiveError) throw options.ensureLiveError;
		return session as unknown as AgentRef["session"];
	});
	const release = vi.fn(async () => {
		if (options.releaseError) throw options.releaseError;
		return true;
	});
	const steerErrors: RpcResponse[] = [];
	// Resolved exactly when onSteerError fires — tests await this real signal
	// instead of sleeping against the clock.
	let signalSteerError: (() => void) | undefined;
	const steerErrorSignal = new Promise<void>(resolve => {
		signalSteerError = resolve;
	});
	const dispatch = (command: RpcSubagentControlCommand) =>
		dispatchRpcSubagentControl(command, {
			lifecycle: { ensureLive, release } as unknown as Pick<AgentLifecycleManager, "ensureLive" | "release">,
			registry: { get: (id: string) => (id === ref.id ? ref : undefined) },
			onSteerError: response => {
				steerErrors.push(response);
				signalSteerError?.();
			},
		});
	return { ref, prompt, abort, ensureLive, release, steerErrors, steerErrorSignal, dispatch };
}

const steer = (text: string, subagentId = "Worker"): RpcSubagentControlCommand => ({
	id: "req-1",
	type: "subagent_steer",
	subagentId,
	text,
});

const abortCommand = (reason: string | undefined, subagentId = "Worker"): RpcSubagentControlCommand => ({
	id: "req-2",
	type: "subagent_abort",
	subagentId,
	...(reason === undefined ? {} : { reason }),
});

describe("rpc subagent control: subagent_steer", () => {
	it("resolves the live session and queues the text as a steering prompt", async () => {
		const h = createHarness();
		const response = await h.dispatch(steer("  focus on the tests  "));
		expect(response).toEqual({ id: "req-1", type: "response", command: "subagent_steer", success: true });
		expect(h.ensureLive).toHaveBeenCalledWith("Worker");
		expect(h.prompt).toHaveBeenCalledWith("focus on the tests", { streamingBehavior: "steer" });
		expect(h.release).not.toHaveBeenCalled();
	});

	it("refuses an unknown id without touching the lifecycle manager", async () => {
		const h = createHarness();
		const response = await h.dispatch(steer("go", "Ghost"));
		expect(response.success).toBe(false);
		if (!response.success) {
			expect(response.code).toBe("unknown_subagent");
			expect(response.error).toContain("Ghost");
		}
		expect(h.ensureLive).not.toHaveBeenCalled();
	});

	it("refuses advisor transcripts and the main session", async () => {
		const advisor = createHarness({ kind: "advisor" });
		const advisorResponse = await advisor.dispatch(steer("hi"));
		expect(advisorResponse.success).toBe(false);
		if (!advisorResponse.success) expect(advisorResponse.code).toBe("advisor_readonly");

		const main = createHarness({ kind: "main" });
		const mainResponse = await main.dispatch(abortCommand("kill"));
		expect(mainResponse.success).toBe(false);
		if (!mainResponse.success) expect(mainResponse.code).toBe("main_session");
	});

	it("refuses empty steer text", async () => {
		const h = createHarness();
		const response = await h.dispatch(steer("   "));
		expect(response.success).toBe(false);
		if (!response.success) expect(response.code).toBe("empty_text");
		expect(h.ensureLive).not.toHaveBeenCalled();
	});

	it("answers success immediately and reports a late revive failure with the isolated explanation", async () => {
		const h = createHarness({
			status: "parked",
			withSession: false,
			ensureLiveError: new Error('Agent "Worker" is parked and cannot be revived (no reviver registered).'),
		});
		const response = await h.dispatch(steer("continue"));
		expect(response).toEqual({ id: "req-1", type: "response", command: "subagent_steer", success: true });
		await h.steerErrorSignal;
		expect(h.steerErrors.length).toBe(1);
		const late = h.steerErrors[0]!;
		expect(late.success).toBe(false);
		if (!late.success) {
			expect(late.id).toBe("req-1");
			expect(late.command).toBe("subagent_steer");
			expect(late.code).toBe("steer_failed");
			expect(late.error).toContain("isolated subagents are terminal");
		}
		expect(h.prompt).not.toHaveBeenCalled();
	});

	it("passes a non-isolated revive failure through unchanged", async () => {
		const h = createHarness({
			status: "parked",
			withSession: false,
			ensureLiveError: new Error("model registry unavailable"),
		});
		await h.dispatch(steer("continue"));
		await h.steerErrorSignal;
		const late = h.steerErrors[0]!;
		if (!late.success) {
			expect(late.code).toBe("steer_failed");
			expect(late.error).toBe("model registry unavailable");
		}
	});
});

describe("rpc subagent control: subagent_abort", () => {
	it("aborts a running turn and releases through the lifecycle owner with a tombstone", async () => {
		const h = createHarness();
		const response = await h.dispatch(abortCommand("user stop"));
		expect(response).toEqual({
			id: "req-2",
			type: "response",
			command: "subagent_abort",
			success: true,
			data: { aborted: true },
		});
		expect(h.abort).toHaveBeenCalledWith({ reason: "user stop" });
		expect(h.release).toHaveBeenCalledWith("Worker", h.ref, { tombstone: true });
	});

	it("defaults the abort reason to the user-interrupt label", async () => {
		const h = createHarness();
		await h.dispatch(abortCommand(undefined));
		expect(h.abort).toHaveBeenCalledWith({ reason: USER_INTERRUPT_LABEL });
	});

	it("skips session abort for a parked ref but still tombstones it", async () => {
		const h = createHarness({ status: "parked", withSession: false });
		const response = await h.dispatch(abortCommand("kill"));
		expect(response.success).toBe(true);
		expect(h.abort).not.toHaveBeenCalled();
		expect(h.release).toHaveBeenCalledWith("Worker", h.ref, { tombstone: true });
	});

	it("surfaces release failures as abort_failed", async () => {
		const h = createHarness({ releaseError: new Error("tombstone write failed") });
		const response = await h.dispatch(abortCommand("kill"));
		expect(response.success).toBe(false);
		if (!response.success) {
			expect(response.code).toBe("abort_failed");
			expect(response.error).toContain("tombstone write failed");
		}
	});

	it("refuses an unknown id", async () => {
		const h = createHarness();
		const response = await h.dispatch(abortCommand("kill", "Ghost"));
		expect(response.success).toBe(false);
		if (!response.success) expect(response.code).toBe("unknown_subagent");
		expect(h.abort).not.toHaveBeenCalled();
		expect(h.release).not.toHaveBeenCalled();
	});
});
