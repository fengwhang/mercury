import {expect, describe, it} from "vitest";

import msgInput from "../../../server/plugins/inputs/msg";

interface SentCall {
	target: string;
	text: string;
	tags?: Record<string, string>;
}

interface FakeIrc {
	network: {cap: {isEnabled: (cap: string) => boolean}};
	say: (target: string, text: string, tags?: Record<string, string>) => void;
	raw: (...parts: string[]) => void;
	emit: (event: string, data: {nick: string; message: string}) => void;
	user: {nick: string; username: string; host: string};
}

interface FakeHarness {
	network: {
		irc: FakeIrc;
		getChannel: (name: string) => {name: string};
	};
	chan: {name: string};
	said: SentCall[];
	rawFrames: string[][];
	emitted: {event: string; data: {nick: string; message: string}}[];
}

// Mercury: multiline input must go out as one draft/multiline batch
// (single visual message), never N dribbles — and legacy servers must
// see byte-identical behavior to before.
function fakeNetwork({multilineCap = true, echoCap = true} = {}): FakeHarness {
	const said: SentCall[] = [];
	const rawFrames: string[][] = [];
	const emitted: {event: string; data: {nick: string; message: string}}[] = [];
	const irc: FakeIrc = {
		network: {
			cap: {
				isEnabled: (cap: string) =>
					cap === "draft/multiline"
						? multilineCap
						: cap === "echo-message"
							? echoCap
							: false,
			},
		},
		say: (target: string, text: string, tags?: Record<string, string>) => {
			said.push({target, text, tags});
		},
		raw: (...parts: string[]) => {
			rawFrames.push(parts);
		},
		emit: (event: string, data: {nick: string; message: string}) => {
			emitted.push({event, data});
		},
		user: {nick: "owner", username: "owner", host: "h"},
	};
	const network = {
		irc,
		getChannel: (name: string) => ({name}),
	};
	const chan = {name: "#test"};
	return {network, chan, said, rawFrames, emitted};
}

describe("Mercury multiline input batching", () => {
	it("sends a paste as one BATCH with tagged lines", () => {
		const {network, chan, said, rawFrames} = fakeNetwork();
		msgInput.input.call(
			{},
			network,
			chan,
			"say",
			["line", "one\nline", "two"]
		);
		expect(rawFrames[0][0]).toBe("BATCH");
		const ref = rawFrames[0][1].slice(1);
		expect(said).toHaveLength(2);
		expect(said[0]).toEqual(
			{target: "#test", text: "line one", tags: {batch: ref}});
		expect(said[1]).toEqual(
			{target: "#test", text: "line two", tags: {batch: ref}});
		expect(rawFrames[rawFrames.length - 1]).toEqual(["BATCH", `-${ref}`]);
	});

	it("sends single lines exactly as before (no frames)", () => {
		const {network, chan, said, rawFrames} = fakeNetwork();
		msgInput.input.call({}, network, chan, "say", ["just one line"]);

		expect(said).toHaveLength(1);
		expect(said[0].text).toBe("just one line");
		expect(rawFrames).toHaveLength(0);
	});

	it("falls back to plain sends without the cap", () => {
		const {network, chan, said, rawFrames} = fakeNetwork({
			multilineCap: false,
		});
		msgInput.input.call({}, network, chan, "say", ["a\nb"]);

		expect(rawFrames).toHaveLength(0);
		expect(said).toHaveLength(1);
	});
});
