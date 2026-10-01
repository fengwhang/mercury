import {expect, describe, it, vi} from "vitest";

import handler from "../../../server/plugins/irc-events/message";
import {Client as IrcClient} from "irc-framework";

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
	mercuryKind?: string;
}

interface FakeChan {
	type: string;
	getUser: (nick: string) => {nick: string};
	findUser: (nick: string) => {nick: string} | undefined;
	pushMessage: (client: unknown, msg: FakePushed) => void;
}

function drive(realClient?: IrcClient) {
	const listeners = new Map<string, Array<(data: FakeLine) => void>>();
	const irc = {
		on(event: string, fn: (data: FakeLine) => void) {
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
		pushMessage(_client: unknown, msg: FakePushed) {
			pushed.push(msg);
		},
	};
	const network = {
		host: "testnet",
		getChannel: (_name: string) => chan,
		getLobby: () => chan,
		isIgnoredUser: () => false,
	};
	handler.call({}, realClient ?? irc, network);

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
	it("retains kind and continuation tags through the real IRC parser", async () => {
		const client = new IrcClient();
		const {pushed} = drive(client);

		for (const wire of [
			"BATCH +real draft/multiline #test",
			'@batch=real;+mercury/kind=assistant_reply :agent!u@h PRIVMSG #test :echo "$A-',
			'@batch=real;+mercury/kind=assistant_reply;draft/multiline-concat :agent!u@h PRIVMSG #test :$B" *.txt',
			"@batch=real;+mercury/kind=assistant_reply;+mercury/empty=1 :agent!u@h PRIVMSG #test : ",
			"BATCH -real",
		]) {
			(client as any).connection.addReadBuffer(wire);
		}

		await vi.waitFor(() => expect(pushed).toHaveLength(1));
		expect(pushed[0].text).toBe('echo "$A-$B" *.txt\n');
		expect(pushed[0].mercuryKind).toBe("assistant_reply");
	});
	it("preserves rendering provenance, blank lines and byte-wrapped continuations", () => {
		const {emit, pushed} = drive();
		const ref: BatchRef = {id: "code", type: "draft/multiline"};

		for (const [text, tags] of [
			['  echo "$A-', {"+mercury/kind": "assistant_reply"}],
			['$B" *.txt  ', {"+mercury/kind": "assistant_reply", "draft/multiline-concat": ""}],
			[" ", {"+mercury/kind": "assistant_reply", "+mercury/empty": "1"}],
			["next", {"+mercury/kind": "assistant_reply"}],
		] as Array<[string, Record<string, string>]>) {
			emit("privmsg", {...line("agent", text, ref), tags});
		}

		emit("batch end draft/multiline", {id: "code"});
		expect(pushed).toHaveLength(1);
		expect(pushed[0].text).toBe('  echo "$A-$B" *.txt  \n\nnext');
		expect(pushed[0].mercuryKind).toBe("assistant_reply");
	});

	it("keeps unprefixed trace kinds and ignores unknown rendering kinds", () => {
		const {emit, pushed} = drive();
		emit("privmsg", {...line("agent", "echo $A-$B"), tags: {"+mercury/kind": "tool_input"}});
		emit("privmsg", {...line("agent", "hello"), tags: {"+mercury/kind": "unknown"}});
		expect(pushed[0].mercuryKind).toBe("tool_input");
		expect(pushed[1].mercuryKind).toBeUndefined();
	});
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
