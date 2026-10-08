import {expect, describe, it} from "vitest";

import Client from "../../../server/client";

// Mercury: plain multiline input must reach inputLine as ONE dispatch
// (so the send path batches it into a single visual message and history
// recalls the entire message). Command/slash lines keep per-line split.
function dispatchCalls(text: string): string[] {
	const seen: string[] = [];
	const fakeThis = {
		inputLine(data: {text: string}) {
			seen.push(data.text);
		},
	};
	(Client.prototype as any).input.call(fakeThis, {
		target: "#test",
		text,
	});
	return seen;
}

describe("Mercury multiline input dispatch", () => {
	it("dispatches plain multiline text whole", () => {
		expect(dispatchCalls("line one\nline two\n\npara two")).toEqual([
			"line one\nline two\n\npara two",
		]);
	});

	it("still splits single lines trivially", () => {
		expect(dispatchCalls("just one")).toEqual(["just one"]);
	});

	it("splits per line when any line is a command", () => {
		expect(dispatchCalls("hello\n/join #x")).toEqual(["hello", "/join #x"]);
	});

	it("splits per line when any line is slash-escaped", () => {
		expect(dispatchCalls("a\n//literal")).toEqual(["a", "//literal"]);
	});
});
