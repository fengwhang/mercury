import { Agent } from "@oh-my-pi/pi-agent-core";
import { HubScopeClient, runNativeHubServer } from "../../src/mirc/external-hub";
import { AgentRegistry } from "../../src/registry/agent-registry";
import type { AgentSession } from "../../src/session/agent-session";
import { MircBridge } from "../../src/session/mirc-bridge";
import { SessionManager } from "../../src/session/session-manager";
import type { MircMessage } from "../../src/mirc/bus";
import { MircBus } from "../../src/mirc/bus";
import { Settings } from "../../src/config/settings";
import { HubTool } from "../../src/tools/hub";
import type { ToolSession } from "../../src/tools";

if (process.argv[2] === "server") await runNativeHubServer();
else {
	const registry = AgentRegistry.global();
	const id = process.env.MERCURY_A2A_ID || process.argv[2];
	const parentId = process.env.MERCURY_A2A_PARENT || "Main";
	const received: MircMessage[] = [];
	const records: string[] = [];
	const settings = Settings.isolated({ "async.enabled": true });
	const nativeAgent = new Agent({ initialState: { systemPrompt: [], messages: [], tools: [] } });
	const bridge = new MircBridge({
		agent: nativeAgent,
		settings,
		sessionManager: SessionManager.inMemory("/tmp"),
		isDisposed: () => false,
		isStreaming: () => true,
		planModeEnabled: () => false,
		emitSessionEvent: async event => {
			if (event.type !== "irc_message") return;
			records.push(String(event.message.content));
			const details = event.message.details as { id: string; from: string; message: string; replyTo?: string };
			received.push({
				id: details.id,
				from: details.from,
				to: id,
				body: details.message,
				ts: event.message.timestamp,
			});
			if (details.message === "held_pending") process.stdout.write("accepted\n");
			if (details.message === "ask") {
				bridge.trackReply(
					MircBus.global()
						.send({ from: id, to: details.from, body: "source-fixture-answer", replyTo: details.id })
						.then(() => {}),
				);
			}
		},
		wakeForMirc: () => {
			throw new Error("Busy fixture must not wake a provider turn");
		},
		runEphemeralTurn: async () => {
			throw new Error("Fixture must never invoke a paid provider");
		},
	});
	registry.register({
		id,
		displayName: id,
		parentId,
		kind: "sub",
		session: {
			isStreaming: true,
			deliverMircMessage: bridge.deliver.bind(bridge),
			drainPendingMircInboxMessages: (agentId: string) => bridge.drainInboxMessages(agentId),
			subscribe: () => () => {},
			waitForMircReplies: bridge.waitForReplies.bind(bridge),
		} as unknown as AgentSession,
	});
	const address = process.env.MERCURY_A2A_ADDRESS;
	const token = process.env.MERCURY_A2A_TOKEN;
	const client =
		address && token ? await HubScopeClient.connect(address, token, registry, MircBus.global()) : undefined;
	const tool = new HubTool({
		settings,
		agentRegistry: registry,
		getAgentId: () => id,
		getSessionFile: () => null,
		getSessionSpawns: () => "*",
		cwd: "/tmp",
		hasUI: false,
	} as ToolSession);
	if (process.argv[3] === "linger") {
		process.stdout.write("ready\n");
		await Bun.stdin.text();
	} else {
		const target = process.argv[3];
		const deadline = Date.now() + 5000;
		while (client && !registry.get(target) && Date.now() < deadline) {
			await Bun.sleep(10);
			await client.flush();
		}
		const sent = await tool.execute("fixture-dm", { op: "send", to: target, message: `hello-from-${id}` });
		const broadcast = await tool.execute("fixture-broadcast", {
			op: "send",
			to: "all",
			message: `broadcast-from-${id}`,
		});
		const listed = await tool.execute("fixture-list", { op: "list" });
		while (client && received.length < 2 && Date.now() < deadline) await Bun.sleep(10);
		const sentDetails = sent.details && "receipts" in sent.details ? sent.details : undefined;
		const listDetails = listed.details && "peers" in listed.details ? listed.details : undefined;
		process.stdout.write(
			`${JSON.stringify({
				id,
				receipt: sentDetails?.receipts?.[0],
				broadcast: broadcast.details,
				received,
				records,
				peers: listDetails?.peers?.map(ref => ref.id),
			})}\n`,
		);
	}
	await bridge.waitForReplies();
	client?.close();
}
