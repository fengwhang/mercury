import { parseArgs } from "@oh-my-pi/pi-coding-agent/cli/args";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { runRootCommand } from "@oh-my-pi/pi-coding-agent/main";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import type { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { logger } from "@oh-my-pi/pi-utils";
import { trace } from "@opentelemetry/api";

const cwd = process.argv[2];
const rawArgs = ["--cwd", cwd, "--print", "--no-session"];
const parsed = parseArgs(rawArgs);
parsed.noExtensions = true;
parsed.noSkills = true;
parsed.noRules = true;
parsed.noTools = true;
parsed.noLsp = true;
const authStorage = await AuthStorage.create(`${cwd}/auth.db`);
const stop = new Error("stop before provider/session creation");
let sessionManager: SessionManager | undefined;
let reachedSessionCreation = false;
let automaticTelemetry = false;
try {
	await runRootCommand(parsed, rawArgs, {
		discoverAuthStorage: async () => authStorage,
		settings: Settings.isolated({ "marketplace.autoUpdate": "off" }),
		createAgentSession: async options => {
			reachedSessionCreation = true;
			sessionManager = options?.sessionManager;
			automaticTelemetry = options?.telemetry !== undefined;
			throw stop;
		},
	});
} catch (error) {
	if (error !== stop) throw error;
} finally {
	authStorage.close();
	await sessionManager?.close();
}

const span = trace.getTracer("@oh-my-pi/pi-agent-core").startSpan("local-only-probe");
const recording = span.isRecording();
span.end();
logger.info("local-only-probe");
await Bun.sleep(250);
console.log(JSON.stringify({ reachedSessionCreation, automaticTelemetry, recording }));
