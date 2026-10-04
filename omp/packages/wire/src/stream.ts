/**
 * Local recording/replay wire shapes for `.ompcast` session recordings
 * (`omp play` and the `/record` capture path).
 *
 * Scoped port of upstream `packages/wire/src/stream.ts`: only the shapes the
 * local recording subset (`packages/coding-agent/src/stream/{protocol,
 * paint-encoder, recording, player}.ts`) needs. The live-broadcast and
 * clip-upload protocol (channel URLs, `live.omp.sh` routes, stencil auth,
 * `ClipUploadResponse`) is intentionally excluded — Mercury does not host
 * public live channels or clips (Observatory/mLounge own sharing).
 */

/** Rows retained per screen frame batch. */
export const STREAM_HISTORY_LIMIT = 2000;

/** One terminal row: ANSI text limited to SGR + OSC 8, width-truncated. */
export type StreamRow = string;
export interface StreamChatMessage {
	/** Monotonic per channel-session; viewers use it for de-duplication. */
	id: number;
	name: string;
	text: string;
	/** Unix milliseconds. */
	ts: number;
	/** Set when the streamer sent it. */
	host?: boolean;
}
