import { type } from "@oh-my-pi/omptype";
import type { AgentTool, AgentToolResult } from "@oh-my-pi/pi-agent-core";

const shareFileSchema = type({
	path: type("string").describe("Absolute local path of the file to share."),
	"caption?": type("string").describe("Optional one-line caption for the chat message."),
});

type ShareFileParams = typeof shareFileSchema.infer;
interface ShareFileDetails {
	url: string;
	filename: string;
}

export class ShareFileTool implements AgentTool<typeof shareFileSchema, ShareFileDetails> {
	readonly name = "share_file";
	readonly approval = "read" as const;
	readonly label = "Share file (unavailable in this nightly)";
	readonly loadMode = "essential";
	readonly description =
		"OMP file publication is excluded from this human-testing nightly pending authorization and secret-path review.";
	readonly parameters = shareFileSchema;

	async execute(_toolCallId: string, _params: ShareFileParams): Promise<AgentToolResult<ShareFileDetails>> {
		return {
			content: [{ type: "text", text: this.description }],
			isError: true,
			details: { url: "", filename: "" },
		};
	}
}
