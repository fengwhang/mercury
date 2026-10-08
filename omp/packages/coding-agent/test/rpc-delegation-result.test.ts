import { describe, expect, test } from "bun:test";
import { recordRpcDelegationEvent } from "../src/modes/rpc/rpc-delegation-result";

function fixture(stopReason = "stop", text = "verified task result") {
	const writes: Array<{ type: string; data?: unknown }> = [];
	const session = {
		getLastAssistantMessage: () => ({
			stopReason,
			content: [{ type: "text" as const, text }],
			errorMessage: stopReason === "error" ? "real provider error" : undefined,
		}),
		sessionManager: {
			appendCustomEntry(type: string, data?: unknown) {
				writes.push({ type, data });
			},
			flushSync() {
				writes.push({ type: "flush" });
			},
		},
	};
	return { session, writes };
}

describe("Hermes-owned RPC task checkpoints", () => {
	test("durably binds start and terminal outcome to the exact child before wire delivery", () => {
		const { session, writes } = fixture();
		recordRpcDelegationEvent(session, { type: "agent_start" }, "deleg_fixture/0");
		recordRpcDelegationEvent(session, { type: "agent_end", isTerminal: true }, "deleg_fixture/0");
		expect(writes.map(write => write.type)).toEqual([
			"mercury_delegation_started",
			"flush",
			"mercury_delegation_terminal",
			"flush",
		]);
		expect(writes[2].data).toEqual({
			childId: "deleg_fixture/0",
			status: "completed",
			summary: "verified task result",
			error: null,
		});
	});

	test("final message and nonterminal agent end never close queued continuation", () => {
		const { session, writes } = fixture();
		recordRpcDelegationEvent(session, { type: "message_end" }, "deleg_fixture/0");
		recordRpcDelegationEvent(session, { type: "agent_end", isTerminal: false }, "deleg_fixture/0");
		expect(writes).toEqual([]);
	});

	for (const [reason, status] of [
		["aborted", "interrupted"],
		["error", "failed"],
		["toolUse", "failed"],
	]) {
		test(`${reason} is ${status}, never successful output`, () => {
			const { session, writes } = fixture(reason);
			recordRpcDelegationEvent(session, { type: "agent_end" }, "deleg_fixture/0");
			expect(writes[0].data).toMatchObject({ status, summary: null });
		});
	}

	test("unbound RPC sessions perform no checkpoint writes", () => {
		const { session, writes } = fixture();
		recordRpcDelegationEvent(session, { type: "agent_end" }, undefined);
		expect(writes).toEqual([]);
	});

	test("a failed durability barrier cannot report success", () => {
		const { session } = fixture();
		session.sessionManager.flushSync = () => {
			throw new Error("fixture fsync failure");
		};
		expect(() => recordRpcDelegationEvent(session, { type: "agent_end" }, "deleg_fixture/0")).toThrow(
			"fixture fsync failure",
		);
	});
});
