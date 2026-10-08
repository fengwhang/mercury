import { afterEach, expect, test } from "bun:test";
import { AgentRegistry } from "../../src/registry/agent-registry";
import { AgentLifecycleManager } from "../../src/registry/agent-lifecycle";
import { MircBus } from "../../src/mirc/bus";
import { NativeHubServer, HubScopeClient } from "../../src/mirc/external-hub";
import type { AgentSession } from "../../src/session/agent-session";
import type { MircMessage } from "../../src/mirc/bus";
import type { AgentSessionEvent } from "../../src/session/agent-session-events";
import type { MircDeliveryReceipt } from "../../src/mirc/bus";
import type { ToolSession } from "../../src/tools";
import { HubTool } from "../../src/tools/hub";
import { Settings } from "../../src/config/settings";
import { executeSend } from "../../src/tools/hub/messaging";

const cleanup: Array<() => Promise<void> | void> = [];
afterEach(async () => {
	for (const close of cleanup.splice(0).reverse()) await close();
});

async function scope() {
	const server = await NativeHubServer.start();
	cleanup.push(() => server.close());
	return server;
}
async function peer(server: NativeHubServer, id: string, parentId?: string) {
	const registry = new AgentRegistry();
	const received: MircMessage[] = [];
	const listeners = new Set<(event: AgentSessionEvent) => void>();
	const token = id === "Main" ? server.ownerToken : server.issue(id, parentId);
	const session = {
		isStreaming: true,
		deliverMircMessage: async (msg: MircMessage) => {
			received.push(msg);
			return "injected";
		},
		subscribe: (listener: (event: AgentSessionEvent) => void) => {
			listeners.add(listener);
			return () => {
				listeners.delete(listener);
			};
		},
		waitForMircReplies: async () => {},
		emitMircRelayObservation: () => {},
		dispose: async () => {},
	} as unknown as AgentSession;
	registry.register({ id, displayName: id, kind: id === "Main" ? "main" : "sub", parentId, session });
	const lifecycle = new AgentLifecycleManager(registry);
	cleanup.push(() => lifecycle.dispose());
	const bus = new MircBus(registry, lifecycle);
	const client = await HubScopeClient.connect(server.address, token, registry, bus);
	cleanup.push(() => client.close());
	const settings = Settings.isolated();
	const tool = new HubTool({
		settings,
		agentRegistry: registry,
		getAgentId: () => id,
		getSessionFile: () => null,
		getSessionSpawns: () => "*",
		cwd: "/tmp",
		hasUI: false,
	} as ToolSession);
	const endTurn = (isTerminal = true) => {
		for (const listener of listeners) listener({ type: "agent_end", messages: [], isTerminal });
	};
	return { registry, received, session, client, bus, tool, settings, token, endTurn, lifecycle };
}

test("Hermes OMP siblings have native mailbox delivery across process registries", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	await l.client.flush();
	expect((await l.bus.send({ from: "Left", to: "Right", body: "sibling-roundtrip" })).outcome).toBe("injected");
	expect(r.received.map(msg => msg.body)).toEqual(["sibling-roundtrip"]);
});

test("external sends retain native sentSince wake relay deduplication", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	await peer(server, "Right", "Main");
	await l.bus.send({ from: "Left", to: "Right", body: "answered" });
	expect(l.bus.sentSince("Left", "Right", 0)).toBe(true);
});

test("flat native membership includes root parents grandchildren and cousins, never other scopes", async () => {
	const server = await scope();
	await peer(server, "Main");
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	l.registry.register({
		id: "Grandchild",
		displayName: "Grandchild",
		kind: "sub",
		parentId: "Left",
		session: l.session,
	});
	r.registry.register({ id: "Cousin", displayName: "Cousin", kind: "sub", parentId: "Right", session: r.session });
	await l.client.flush();
	await r.client.flush();
	await l.client.flush();
	expect(
		l.registry
			.listVisibleTo("Grandchild")
			.map(ref => ref.id)
			.sort(),
	).toEqual(["Cousin", "Left", "Main", "Right"]);
	const other = await scope();
	const stranger = await peer(other, "Stranger", "Main");
	expect((await l.bus.send({ from: "Left", to: "Stranger", body: "private" })).outcome).toBe("failed");
	expect(stranger.received).toEqual([]);
	expect(l.registry.listVisibleTo("Left").some(ref => ref.id === "Stranger")).toBe(false);
});

test("sender identity cannot impersonate parent or issue from another subtree", async () => {
	const server = await scope();
	await peer(server, "Main");
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	await expect(l.bus.send({ from: "Main", to: "Right", body: "forged-owner" })).rejects.toThrow("Sender capability");
	expect(r.received).toEqual([]);
});

test("external transport rejects non-loopback and implicit-host addresses before connecting", async () => {
	for (const address of ["localhost:1", "[::1]:1", "192.0.2.1:1", "https://127.0.0.1:1"]) {
		await expect(HubScopeClient.connect(address, "invalid-fixture-grant", new AgentRegistry())).rejects.toThrow(
			"localhost",
		);
	}
});

test("reserved sibling root cannot be registered by another granted subtree", async () => {
	const server = await scope();
	const left = await peer(server, "Left", "Main");
	server.issue("Right", "Main");
	left.registry.register({ id: "Right", displayName: "Right", kind: "sub", parentId: "Left", session: left.session });
	await expect(left.client.flush()).rejects.toThrow("reserved");
	expect(server.registry.get("Right")).toBeUndefined();
});

test("involuntary disconnect clears mirrors and settles accepted unbounded await", async () => {
	const server = await scope();
	const left = await peer(server, "Left", "Main");
	await peer(server, "Right", "Main");
	await left.client.flush();
	const accepted = Promise.withResolvers<void>();
	const send = left.bus.send.bind(left.bus);
	left.bus.send = async (...args) => {
		const receipt = await send(...args);
		accepted.resolve();
		return receipt;
	};
	const pending = executeSend(
		{ registry: left.registry, senderId: "Left", settings: left.settings, bus: left.bus },
		{ to: "Right", message: "question", await: true, timeoutMs: 0 },
	);
	await accepted.promise;
	await server.close();
	// A bounded deadline detects a missing real socket-close event; fake time
	// cannot drive OS TCP shutdown. This is not a scheduling sleep.
	const settled = await Promise.race([pending.then(() => true), Bun.sleep(200).then(() => false)]);
	// Always settle the test's own outstanding request, including on the red run.
	left.client.close();
	await pending;
	expect(settled).toBe(true);
	expect(left.registry.get("Right")).toBeUndefined();
});

test("successful transport receipt is not duplicated into native inbox", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	const waiting = r.bus.wait("Right", { from: "Left" }, 1000);
	await l.bus.send({ from: "Left", to: "Right", body: "one-delivery" });
	const message = await waiting;
	expect(message?.body).toBe("one-delivery");
	expect(r.received).toEqual([]);
	expect(r.bus.inbox("Right")).toEqual([]);
});

test("native receive exception buffers once and reports failed receipt", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	r.session.deliverMircMessage = async () => {
		throw new Error("fixture-disposed");
	};
	expect((await l.bus.send({ from: "Left", to: "Right", body: "recoverable" })).outcome).toBe("failed");
	expect(r.bus.inbox("Right").map(msg => msg.body)).toEqual(["recoverable"]);
	expect(r.bus.inbox("Right")).toEqual([]);
});

test("completion cancellation and generation replacement remove only their owned membership", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	r.registry.unregister("Right");
	await r.client.flush();
	await l.client.flush();
	expect(l.registry.get("Right")).toBeUndefined();
	expect((await l.bus.send({ from: "Left", to: "Right", body: "late" })).outcome).toBe("failed");
	const newRegistry = new AgentRegistry();
	newRegistry.register({ id: "Right", displayName: "Right", kind: "sub", parentId: "Main", session: r.session });
	const replacement = await HubScopeClient.connect(server.address, r.token, newRegistry);
	cleanup.push(() => replacement.close());
	r.client.close();
	await replacement.flush();
	await l.client.flush();
	expect((await l.bus.send({ from: "Left", to: "Right", body: "reconnected-once" })).outcome).toBe("injected");
	expect(r.received.map(msg => msg.body)).toEqual(["reconnected-once"]);
});

test("native awaited sibling send consumes a real reply through mirrored sessions", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	await l.client.flush();
	let reply: Promise<MircDeliveryReceipt> | undefined;
	r.session.deliverMircMessage = async message => {
		r.received.push(message);
		reply = r.bus.send({ from: "Right", to: "Left", body: "reply", replyTo: message.id });
		return "injected";
	};
	const result = await executeSend(
		{ registry: l.registry, senderId: "Left", settings: l.settings, bus: l.bus },
		{ to: "Right", message: "question", await: true, timeoutMs: 1000 },
	);
	expect(result.details?.waited?.body).toBe("reply");
	await reply;
	expect(l.bus.inbox("Left")).toEqual([]);
});

test("native terminal peer events end an external await without waiting for timeout", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	await l.client.flush();
	r.session.deliverMircMessage = async () => {
		r.endTurn(false);
		queueMicrotask(() => r.endTurn());
		return "injected";
	};
	const result = await executeSend(
		{ registry: l.registry, senderId: "Left", settings: l.settings, bus: l.bus },
		{ to: "Right", message: "question", await: true, timeoutMs: 2000 },
	);
	expect(result.content[0]).toMatchObject({ type: "text", text: expect.stringContaining("stopped without replying") });
});

test("failed remote reply drainage ends an await without an unhandled rejection", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	await l.client.flush();
	r.session.waitForMircReplies = async () => {
		throw new Error("Reply transport disconnected");
	};
	r.session.deliverMircMessage = async () => {
		queueMicrotask(() => r.endTurn());
		return "injected";
	};
	const result = await executeSend(
		{ registry: l.registry, senderId: "Left", settings: l.settings, bus: l.bus },
		{ to: "Right", message: "question", await: true, timeoutMs: 200 },
	);
	expect(result.content[0]).toMatchObject({ type: "text", text: expect.stringContaining("stopped without replying") });
	expect(l.bus.inbox("Left")).toEqual([]);
});

test("remote reply drainage preserves native awaits beyond the transport deadline", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	await l.client.flush();
	let drain: Promise<void> | undefined;
	r.session.waitForMircReplies = () => {
		drain = (async () => {
			await Bun.sleep(10_100);
			await r.bus.send({ from: "Right", to: "Left", body: "slow-real-reply" });
		})();
		return drain;
	};
	r.session.deliverMircMessage = async () => {
		queueMicrotask(() => r.endTurn());
		return "injected";
	};
	const result = await executeSend(
		{ registry: l.registry, senderId: "Left", settings: l.settings, bus: l.bus },
		{ to: "Right", message: "question", await: true, timeoutMs: 15_000 },
	);
	await drain;
	expect(result.details?.waited?.body).toBe("slow-real-reply");
}, 20_000);

test("hard-aborted peer capability cannot resurrect after transport reconnect", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	r.registry.setStatus("Right", "aborted");
	await r.client.flush();
	expect((await l.bus.send({ from: "Left", to: "Right", body: "late" })).outcome).toBe("failed");
	r.client.close();
	const replacement = new AgentRegistry();
	replacement.register({ id: "Right", displayName: "Right", kind: "sub", parentId: "Main", session: r.session });
	await expect(HubScopeClient.connect(server.address, r.token, replacement)).rejects.toThrow(
		"Aborted peer is terminal",
	);
});

test("external delivery uses the destination's native parked revival and exact ref", async () => {
	const server = await scope();
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	let disposed = 0;
	r.session.dispose = async () => {
		disposed++;
	};
	r.registry.setStatus("Right", "idle");
	let revived = 0;
	r.lifecycle.adopt("Right", {
		idleTtlMs: 0,
		revive: async () => {
			revived++;
			const fresh = {
				...r.session,
				isStreaming: false,
				deliverMircMessage: async (message: MircMessage) => {
					r.received.push(message);
					return "injected";
				},
				dispose: async () => {},
			};
			return fresh as unknown as AgentSession;
		},
	});
	await r.lifecycle.park("Right");
	await r.client.flush();
	await l.client.flush();
	expect(disposed).toBe(1);
	expect(l.registry.get("Right")?.status).toBe("parked");
	expect(l.registry.listVisibleTo("Left").some(ref => ref.id === "Right")).toBe(false);
	const receipts = await Promise.all([
		l.bus.send({ from: "Left", to: "Right", body: "revive-a" }),
		l.bus.send({ from: "Left", to: "Right", body: "revive-b" }),
	]);
	expect(receipts.every(receipt => receipt.outcome === "revived")).toBe(true);
	expect(revived).toBe(1);
	expect(r.received.map(message => message.body).sort()).toEqual(["revive-a", "revive-b"]);
	await r.client.flush();
	await l.client.flush();
	expect(l.registry.get("Right")?.status).toBe("idle");
});

test("native and external parent scopes expose identical topology and send receipts", async () => {
	const server = await scope();
	await peer(server, "Main");
	const l = await peer(server, "Left", "Main");
	const r = await peer(server, "Right", "Main");
	await l.client.flush();
	const native = new AgentRegistry();
	for (const ref of server.registry.list()) {
		native.register({
			id: ref.id,
			displayName: ref.displayName,
			kind: ref.kind,
			parentId: ref.parentId,
			session: ref.id === "Right" ? r.session : l.session,
			status: ref.status,
		});
	}
	expect(
		l.registry
			.listVisibleTo("Left")
			.map(ref => [ref.id, ref.parentId, ref.kind, ref.status])
			.sort(),
	).toEqual(
		native
			.listVisibleTo("Left")
			.map(ref => [ref.id, ref.parentId, ref.kind, ref.status])
			.sort(),
	);
	const expected = await new MircBus(native).send({ from: "Left", to: "Right", body: "same-contract" });
	const actual = await l.bus.send({ from: "Left", to: "Right", body: "same-contract" });
	expect(actual).toEqual(expected);
});
