import {expect, describe, it, vi} from "vitest";

import handler from "../../../server/plugins/irc-events/message";

// Mercury: draft/multiline batches must surface as ONE message with the
// lines joined — the single-visual-message contract. Untagged traffic
// flows through untouched.
interface BatchRef {
	id: string;
	type: string;
}

interface FakeLine {
	nick: string;
	ident: string;
	hostname: string;
	target: string;
	message: string;
	tags: Record<string, string>;
	batch?: BatchRef;
}

interface FakePushed {
	text: string;
}

interface FakeChan {
	type: string;
	getUser: (nick: string) => {nick: string};
	findUser: (nick: string) => {nick: string} | undefined;
	pushMessage: (client: unknown, msg: FakePushed) => void;
}

function drive() {
	const listeners = new Map<string, Array<(data: FakeLine) => void>>();
	const irc = {
		on: (event: string, fn: (data: FakeLine) => void) => {
			const existing = listeners.get(event) ?? [];
			existing.push(fn);
			listeners.set(event, existing);
		},
		user: {nick: "me"},
	};
	const pushed: FakePushed[] = [];
	const chan: FakeChan = {
		type: "channel",
		getUser: (nick: string) => ({nick}),
		findUser: (_nick: string) => undefined,
		pushMessage: (_client: unknown, msg: FakePushed) => {
			pushed.push(msg);
		},
	};
	const network = {
		host: "testnet",
		getChannel: (_name: string) => chan,
		getLobby: () => chan,
		isIgnoredUser: () => false,
	};
	handler.call({}, irc, network);
	const emit = (event: string, data: FakeLine | {id: string}) => {
		for (const fn of listeners.get(event) ?? []) {
			fn(data as FakeLine);
		}
	};
	return {emit, pushed};
}

function line(nick: string, text: string, batch?: BatchRef): FakeLine {
	return {
		nick,
		ident: "u",
		hostname: "h",
		target: "#test",
		message: text,
		tags: {},
		...(batch ? {batch} : {}),
	};
}

describe("Mercury multiline inbound reassembly", () => {
	it("joins one batch into a single message", () => {
		const {emit, pushed} = drive();
		const ref: BatchRef = {id: "r1", type: "draft/multiline"};
		emit("privmsg", line("alice", "batched one", ref));
		emit("privmsg", line("alice", "batched two", ref));
		expect(pushed).toHaveLength(0);
		emit("batch end draft/multiline", {id: "r1"});
		expect(pushed).toHaveLength(1);
		expect(pushed[0].text).toBe("batched one\nbatched two");
	});

	it("passes untagged lines straight through", () => {
		const {emit, pushed} = drive();
		emit("privmsg", line("alice", "plain hello"));
		expect(pushed).toHaveLength(1);
		expect(pushed[0].text).toBe("plain hello");
	});
});
