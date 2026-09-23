import { randomBytes } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { type } from "@oh-my-pi/omptype";
import type {
	AgentTool,
	AgentToolContext,
	AgentToolResult,
	AgentToolUpdateCallback,
} from "@oh-my-pi/pi-agent-core";

const shareFileSchema = type({
	path: type("string").describe("Absolute local path of the file to share."),
	"caption?": type("string").describe("Optional one-line caption for the chat message."),
});

type ShareFileParams = typeof shareFileSchema.infer;

interface ShareFileDetails {
	url: string;
	filename: string;
}

const DENIED_PREFIXES = ["/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/var/log"];
const DENIED_HOME_PARTS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".config", ".azure", ".gcloud"];

function fail(message: string): AgentToolResult<ShareFileDetails> {
	return {
		content: [{ type: "text", text: message }],
		details: { url: "", filename: "" },
	};
}

function mercuryHome(): string {
	const home = process.env.MERCURY_HOME?.trim();
	if (home) return home;
	throw new Error("MERCURY_HOME is not set — file sharing needs a provisioned observatory");
}

function loungeHome(): string {
	return join(mercuryHome(), "observatory", "lounge", "home");
}

function loungeBind(): { host: string; port: number } {
	let host = "127.0.0.1";
	let port = 9000;
	try {
		const conf = readFileSync(join(loungeHome(), "config.js"), "utf-8");
		const hostMatch = conf.match(/host:\s*"([^"]+)"/);
		const portMatch = conf.match(/port:\s*(\d+)/);
		if (hostMatch) host = hostMatch[1];
		if (portMatch) port = Number.parseInt(portMatch[1], 10);
	} catch {
		// fall through to defaults
	}
	return { host, port };
}

function checkDenied(resolved: string): string | null {
	for (const prefix of DENIED_PREFIXES) {
		if (resolved === prefix || resolved.startsWith(prefix + "/")) {
			return `refusing system path: ${resolved.slice(0, 200)}`;
		}
	}
	const home = homedir();
	if (resolved.startsWith(home + "/")) {
		const first = resolved.slice(home.length + 1).split("/", 1)[0];
		if (DENIED_HOME_PARTS.includes(first)) {
			return `refusing credential path: ${resolved.slice(0, 200)}`;
		}
	}
	const lowered = basename(resolved).toLowerCase();
	if (lowered === ".env" || lowered.endsWith(".key") || lowered.endsWith(".pem")) {
		return `refusing secret-looking file: ${resolved.slice(0, 200)}`;
	}
	const mercuryRoot = resolve(mercuryHome());
	const uploadsRoot = join(loungeHome(), "uploads");
	if (resolved.startsWith(mercuryRoot + "/") && !resolved.startsWith(uploadsRoot + "/")) {
		return `refusing mercury-home file: ${resolved.slice(0, 200)}`;
	}
	return null;
}

export class ShareFileTool implements AgentTool<typeof shareFileSchema, ShareFileDetails> {
	readonly name = "share_file";
	readonly approval = "read" as const;
	readonly label = "Share file";
	readonly loadMode = "essential";
	readonly description =
		"Share a local file in the current chat (the paperclip button). " +
		"Stages the file as a Lounge upload and returns its URL — post the URL " +
		"in your reply so the user can open it. The link is only visible in " +
		"this room.";
	readonly parameters = shareFileSchema;

	async execute(
		_toolCallId: string,
		params: ShareFileParams,
		_signal?: AbortSignal,
		_onUpdate?: AgentToolUpdateCallback<ShareFileDetails>,
		_context?: AgentToolContext,
	): Promise<AgentToolResult<ShareFileDetails>> {
		const raw = params.path?.trim() ?? "";
		if (!raw) return fail("path is required");
		if (!isAbsolute(raw)) return fail("path must be absolute");
		let resolved: string;
		try {
			resolved = resolve(raw);
			const stat = statSync(resolved);
			if (!stat.isFile()) return fail(`not a regular file: ${raw.slice(0, 200)}`);
		} catch {
			return fail(`file not found: ${raw.slice(0, 200)}`);
		}
		const denied = checkDenied(resolved);
		if (denied) return fail(denied);
	 const token = randomBytes(8).toString("hex");
		const destDir = join(loungeHome(), "uploads", token.slice(0, 2));
		try {
			mkdirSync(destDir, { recursive: true });
			copyFileSync(resolved, join(destDir, token));
		} catch (error) {
			return fail(`upload stage failed: ${error instanceof Error ? error.message : String(error)}`);
		}
		const filename = basename(resolved).slice(0, 128) || "file";
		const { host, port } = loungeBind();
		const url = `http://${host}:${port}/uploads/${token}/${encodeURIComponent(filename)}`;
		const caption = params.caption?.trim();
		const message = caption ? `${caption}\n${url}` : url;
		return {
			content: [{ type: "text", text: `File staged. Post this URL in your reply so the user can open it:\n${message}` }],
			details: { url, filename },
		};
	}
}
