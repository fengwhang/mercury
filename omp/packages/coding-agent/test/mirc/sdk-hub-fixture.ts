import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { createAgentSession } from "../../src/sdk";
import { ModelRegistry } from "../../src/config/model-registry";
import { Settings } from "../../src/config/settings";
import { AgentRegistry } from "../../src/registry/agent-registry";
import { AuthStorage } from "../../src/session/auth-storage";
import { SessionManager } from "../../src/session/session-manager";
import { HubTool } from "../../src/tools/hub";
import type { ToolSession } from "../../src/tools";

// Real SDK, registry, tools and transport. No prompt/model invocation, model
// discovery refresh, installed executable, or real credential storage.
const auth = await AuthStorage.create(":memory:");
const models = new ModelRegistry(auth);
const enabled = process.argv[2] !== "disabled";
const directory = process.argv[3];
const settings = Settings.isolated({ "memory.backend": "off", "autolearn.enabled": false });
const { session } = await createAgentSession({
	cwd: directory,
	agentDir: directory,
	modelRegistry: models,
	model: getBundledModel("anthropic", "claude-sonnet-4-5"),
	settings,
	sessionManager: SessionManager.inMemory(directory),
	toolNames: ["hub"],
	skills: [],
	contextFiles: [],
	promptTemplates: [],
	slashCommands: [],
	disableExtensionDiscovery: true,
	enableMCP: false,
	enableLsp: false,
	enableMirc: enabled,
	skipPythonPreflight: true,
});
try {
	const registry = AgentRegistry.global();
	const id = session.getAgentId()!;
	const ref = registry.get(id)!;
	let sent;
	if (enabled) {
		const tool = new HubTool({
			cwd: directory,
			hasUI: false,
			settings,
			agentRegistry: registry,
			getAgentId: () => id,
			getSessionFile: () => null,
			getSessionSpawns: () => "*",
		} as ToolSession);
		sent = await tool.execute("source-sdk-peer", { op: "send", to: "Main", message: "sdk-source-parent-receipt" });
	}
	process.stdout.write(
		`${JSON.stringify({
			id,
			kind: ref.kind,
			parentId: ref.parentId,
			tools: session.getActiveToolNames(),
			peers: registry.listVisibleTo(id).map(peer => peer.id),
			sent,
		})}\n`,
	);
} finally {
	await session.dispose();
	auth.close();
}
