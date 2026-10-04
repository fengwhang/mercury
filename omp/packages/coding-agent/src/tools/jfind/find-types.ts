/**
 * Plain data shapes for semantic-find results. Mirrored from the `find` tool's
 * transcript types (upstream `packages/tui/src/tools/find.ts`): the CLI and
 * cascade layers below only need these interfaces, not the TUI renderer, so
 * they live here to keep the excluded TUI surface out of the port.
 */

/** A verified line range with its yes-probability and a one-line preview. */
export interface FindRange {
	start: number;
	end: number;
	p: number;
	snippet: string;
}

/** A file whose verified passages cleared the threshold; `ranges` are merged positive spans, strongest first. */
export interface FindHit {
	/** Display path relative to the search cwd, or an internal URL under URL scopes. */
	rel: string;
	/** Filename judgment, when the name batch answered. */
	nameScore?: number;
	/** Best verified passage probability. */
	contentScore: number;
	ranges: FindRange[];
	/** Lines of content actually judged, and whether the file held more. */
	linesSeen: number;
	truncated: boolean;
}

/** Search accounting reported alongside the hits. */
export interface FindStats {
	/** Eligible files under the root. */
	listed: number;
	requests: number;
	errors: number;
	/** Entries judged by name. */
	judged: number;
	/** Files whose content was read and sent. */
	filesRead: number;
	fileBytes: number;
	inputTokens: number;
	outputTokens: number;
	cost: number;
	apiMs: number;
	windowsJudged: number;
	windowsPruned: number;
	mapCards: number;
	/** Distinct request failures, phase-prefixed. */
	failures: string[];
}
