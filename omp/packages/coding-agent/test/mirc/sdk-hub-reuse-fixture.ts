import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { createAgentSession } from "../../src/sdk";
import { ModelRegistry } from "../../src/config/model-registry";
import { Settings } from "../../src/config/settings";
import { AuthStorage } from "../../src/session/auth-storage";
import { SessionManager } from "../../src/session/session-manager";
import { AgentRegistry } from "../../src/registry/agent-registry";

const auth = await AuthStorage.create(":memory:");
const directory = process.argv[2];
try {
	for (let generation = 1; generation <= 2; generation++) {
		const { session } = await createAgentSession({
			cwd: directory,
			agentDir: directory,
			modelRegistry: new ModelRegistry(auth),
			model: getBundledModel("anthropic", "claude-sonnet-4-5"),
			settings: Settings.isolated({
				"memory.backend": "off",
				"autolearn.enabled": false,
				"task.maxRecursionDepth": 0,
			}),
			sessionManager: SessionManager.inMemory(directory),
			toolNames: ["hub"],
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			disableExtensionDiscovery: true,
			enableMCP: false,
			enableLsp: false,
			skipPythonPreflight: true,
		});
		if (session.getAgentId() !== "SDKReuse") throw new Error("External identity lost");
		if (!session.getActiveToolNames().includes("hub")) throw new Error("Hub capability lost");
		await session.dispose();
		if (AgentRegistry.global().get("SDKReuse")) throw new Error("Disposed SDK ownership retained");
	}
	console.log("two SDK generations disposed cleanly");
} finally {
	auth.close();
}
