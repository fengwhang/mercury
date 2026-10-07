/**
 * Fixture builder for Hermes-engine stats tests.
 *
 * The fixture schema is extracted from the Mercury fork's real DDL
 * (`hermes/mercury_state_common.py` `SCHEMA_SQL`) so tests exercise the actual
 * Hermes session store contract rather than a drifting copy of it. Returns
 * null when the fork tree is absent (e.g. a stock upstream OMP checkout), so
 * callers can skip cleanly there.
 */
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";

export const HERMES_FORK_SCHEMA_PATH = path.resolve(
	import.meta.dir,
	"..",
	"..",
	"..",
	"..",
	"..",
	"hermes",
	"mercury_state_common.py",
);

export function hermesSchemaSql(): string | null {
	let source: string;
	try {
		source = fs.readFileSync(HERMES_FORK_SCHEMA_PATH, "utf8");
	} catch {
		return null;
	}
	const match = source.match(/SCHEMA_SQL = """([\s\S]*?)"""/);
	return match ? match[1] : null;
}

export interface HermesFixtureUsage {
	sessionId: string;
	model: string;
	provider: string;
	task?: string;
	apiCalls: number;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	reasoning?: number;
	cost?: number;
	costStatus?: string;
	firstSeenSec: number;
	lastSeenSec: number;
}

export interface HermesFixtureSession {
	id: string;
	startedAtSec: number;
	lastActivityAtSec: number;
	messages?: Array<{ role: string; finishReason: string | null; timestampSec: number }>;
}

export interface HermesFixture {
	sessions: HermesFixtureSession[];
	usage: HermesFixtureUsage[];
}

/**
 * Create `home/state.db` from the real schema and populate the fixture rows.
 * Returns the state db path. Leaves journal mode at the schema default unless
 * `wal` is set (which mimics the live Hermes store).
 */
export function buildHermesFixture(home: string, fixture: HermesFixture, opts?: { wal?: boolean }): string {
	const schemaSql = hermesSchemaSql();
	if (schemaSql === null) {
		throw new Error(`Hermes fork schema not found at ${HERMES_FORK_SCHEMA_PATH}`);
	}
	fs.mkdirSync(home, { recursive: true });
	const stateDb = path.join(home, "state.db");
	const db = new Database(stateDb);
	try {
		if (opts?.wal) db.exec("PRAGMA journal_mode=WAL");
		db.exec(schemaSql);
		for (const session of fixture.sessions) {
			db.query(
				`INSERT INTO sessions (id, source, started_at, last_activity_at, model)
				 VALUES (?, 'cli', ?, ?, 'fixture-model')`,
			).run(session.id, session.startedAtSec, session.lastActivityAtSec);
			for (const message of session.messages ?? []) {
				db.query(
					`INSERT INTO messages (session_id, role, content, finish_reason, timestamp)
				 VALUES (?, ?, 'fixture', ?, ?)`,
				).run(session.id, message.role, message.finishReason, message.timestampSec);
			}
		}
		for (const row of fixture.usage) {
			db.query(
				`INSERT INTO session_model_usage
				 (session_id, model, billing_provider, billing_base_url, billing_mode, task,
				  api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
				  reasoning_tokens, estimated_cost_usd, cost_status, first_seen, last_seen)
				 VALUES (?, ?, ?, '', 'chat_completions', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			).run(
				row.sessionId,
				row.model,
				row.provider,
				row.task ?? "",
				row.apiCalls,
				row.input,
				row.output,
				row.cacheRead,
				row.cacheWrite,
				row.reasoning ?? 0,
				row.cost ?? 0,
				row.costStatus ?? "estimated",
				row.firstSeenSec,
				row.lastSeenSec,
			);
		}
	} finally {
		db.close();
	}
	return stateDb;
}

/** Snapshot of every file in a directory (name → size + content hash). */
export function snapshotDir(dir: string): Map<string, string> {
	const snapshot = new Map<string, string>();
	for (const entry of fs.readdirSync(dir).sort()) {
		const bytes = fs.readFileSync(path.join(dir, entry));
		snapshot.set(entry, `${bytes.length}:${Bun.hash(bytes)}`);
	}
	return snapshot;
}
