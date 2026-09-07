import { describe, expect, it } from "bun:test";
import { assembleYieldResult } from "@oh-my-pi/pi-coding-agent/task/yield-assembly";

// HERMES-OMP PATCH (bug #5 artifact 2): a terminal STRING yield must reach
// the parent RAW. This test pins the contract at the assembly + serialization
// boundary: explicit data strings, use_last_turn strings, and object payloads.

interface YieldItem {
	status?: string;
	type?: string | string[];
	data?: unknown;
	useLastTurn?: boolean;
	schemaOverridden?: boolean;
}

const asYield = (item: YieldItem) => item as never;

function serializeTerminal(completeData: unknown): string {
	// mirror of the executor's patched branch
	return typeof completeData === "string"
		? completeData
		: (JSON.stringify(completeData, null, 2) ?? "null");
}

describe("bug #5 artifact 2 — string yields are raw", () => {
	it("explicit yield(data=\"hello\") serializes to hello, 5 bytes, no quotes", () => {
		const assembled = assembleYieldResult([asYield({ data: "hello" })], undefined, undefined);
		if (!assembled) throw new Error("no assembly");
		const completeData = assembled.data;
		const out = serializeTerminal(completeData);
		expect(out).toBe("hello");
		expect(out.length).toBe(5);
		expect(out.startsWith('"')).toBe(false);
	});

	it("untyped yield with string data is terminal and raw", () => {
		const assembled = assembleYieldResult([asYield({ data: "line1\nline2" })], undefined, undefined);
		if (!assembled) throw new Error("no assembly");
		const out = serializeTerminal(assembled.data);
		expect(out).toBe("line1\nline2");
	});

	it("use_last_turn terminal yield stays raw (regression guard)", () => {
		const assembled = assembleYieldResult(
			[asYield({ type: "result", useLastTurn: true })],
			"final text",
			undefined,
		);
		if (!assembled) throw new Error("no assembly");
		const out = serializeTerminal(assembled.data);
		expect(out).toBe("final text");
	});

	it("object payloads still pretty-print as JSON", () => {
		const assembled = assembleYieldResult(
			[asYield({ data: { answer: 42 } })],
			undefined,
			undefined,
		);
		if (!assembled) throw new Error("no assembly");
		const out = serializeTerminal(assembled.data);
		expect(JSON.parse(out)).toEqual({ answer: 42 });
		expect(out).toContain("\n"); // pretty-printed
	});

	it("sections object serializes as JSON (not raw)", () => {
		const assembled = assembleYieldResult(
			[asYield({ type: ["findings"], data: ["a"] })],
			undefined,
			undefined,
		);
		if (!assembled) throw new Error("no assembly");
		// sections without a terminal -> undefined until finalized; use the
		// sections shape directly
		const sections = assembled.data as Record<string, unknown>;
		expect(sections.findings).toEqual(["a"]);
	});
});
