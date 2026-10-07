import { describe, expect, it } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import { combineEngineAggregates, readHermesEngineStats } from "../src/mercury-engines";
import type { EngineAggregatedStats } from "../src/shared-types";
import { buildHermesFixture, type HermesFixture, hermesSchemaSql, snapshotDir } from "./helpers/hermes-fixture";

const schemaSql = hermesSchemaSql();
const hasForkSchema = schemaSql !== null;

const NOW_SEC = Math.floor(Date.now() / 1000);
const HOUR = 3600;
const DAY = 24 * HOUR;

/** Three sessions: fresh main usage, an old one outside any short window, a failed one. */
const FIXTURE: HermesFixture = {
	sessions: [
		{
			id: "s1",
			startedAtSec: NOW_SEC - HOUR,
			lastActivityAtSec: NOW_SEC - 60,
			messages: [
				{ role: "user", finishReason: null, timestampSec: NOW_SEC - HOUR },
				{ role: "assistant", finishReason: "stop", timestampSec: NOW_SEC - 60 },
			],
		},
		{
			id: "s2",
			startedAtSec: NOW_SEC - 10 * DAY,
			lastActivityAtSec: NOW_SEC - 10 * DAY,
			messages: [{ role: "assistant", finishReason: "stop", timestampSec: NOW_SEC - 10 * DAY }],
		},
		{
			id: "s3",
			startedAtSec: NOW_SEC - 2 * HOUR,
			lastActivityAtSec: NOW_SEC - 30,
			messages: [
				{ role: "user", finishReason: null, timestampSec: NOW_SEC - 2 * HOUR },
				{ role: "assistant", finishReason: "error", timestampSec: NOW_SEC - 30 },
			],
		},
	],
	usage: [
		{
			sessionId: "s1",
			model: "m1",
			provider: "prov-a",
			apiCalls: 10,
			input: 1000,
			output: 200,
			cacheRead: 300,
			cacheWrite: 50,
			reasoning: 25,
			cost: 1.25,
			costStatus: "estimated",
			firstSeenSec: NOW_SEC - HOUR,
			lastSeenSec: NOW_SEC - 60,
		},
		{
			sessionId: "s1",
			model: "m2",
			provider: "",
			task: "title_generation",
			apiCalls: 3,
			input: 300,
			output: 100,
			cacheRead: 0,
			cacheWrite: 0,
			reasoning: 0,
			cost: 0.05,
			costStatus: "included",
			firstSeenSec: NOW_SEC - HOUR,
			lastSeenSec: NOW_SEC - HOUR,
		},
		{
			sessionId: "s2",
			model: "m1",
			provider: "prov-b",
			apiCalls: 7,
			input: 700,
			output: 70,
			cacheRead: 0,
			cacheWrite: 0,
			reasoning: 0,
			cost: 0.7,
			costStatus: "estimated",
			firstSeenSec: NOW_SEC - 10 * DAY,
			lastSeenSec: NOW_SEC - 10 * DAY,
		},
		{
			sessionId: "s3",
			model: "m1",
			provider: "prov-a",
			apiCalls: 2,
			input: 200,
			output: 40,
			cacheRead: 0,
			cacheWrite: 0,
			reasoning: 0,
			cost: 0.2,
			costStatus: "estimated",
			firstSeenSec: NOW_SEC - 2 * HOUR,
			lastSeenSec: NOW_SEC - 30,
		},
	],
};

function withFixture(fn: (home: string) => void, opts?: { wal?: boolean }): void {
	const temp = TempDir.createSync("@pi-stats-hermes-fixture-");
	try {
		buildHermesFixture(temp.join("hermes"), FIXTURE, opts);
		fn(temp.join("hermes"));
	} finally {
		temp.removeSync();
	}
}

describe.skipIf(!hasForkSchema)("Hermes engine stats from the real session store", () => {
	it("matches hand-computed fixture totals for every derived metric", () => {
		withFixture(home => {
			const { stats } = readHermesEngineStats(home, null);
			expect(stats.label).toBe("Hermes");
			// Requests/tokens/cost come from session_model_usage rows (all tasks).
			expect(stats.overall.totalRequests).toBe(22);
			expect(stats.overall.totalInputTokens).toBe(2200);
			expect(stats.overall.totalOutputTokens).toBe(410);
			expect(stats.overall.totalCacheReadTokens).toBe(300);
			expect(stats.overall.totalCacheWriteTokens).toBe(50);
			expect(stats.overall.totalReasoningTokens).toBe(25);
			expect(stats.overall.totalCost).toBeCloseTo(2.2, 6);
			expect(stats.overall.includedRequests).toBe(3);
			// cacheRate = cacheRead / (input + cacheRead), the dashboard's formula.
			expect(stats.overall.cacheRate).toBeCloseTo(300 / 2500, 6);
			expect(stats.sessions).toBe(3);
			expect(stats.overall.errorSessions).toBe(1);
			expect(stats.overall.firstTimestamp).toBe((NOW_SEC - 10 * DAY) * 1000);
			expect(stats.overall.lastTimestamp).toBe((NOW_SEC - 30) * 1000);
		});
	});

	it("attributes usage per model and provider without conflating routes", () => {
		withFixture(home => {
			const { byModel, byProvider } = readHermesEngineStats(home, null);
			expect(byModel).toHaveLength(3);
			const m1a = byModel.find(row => row.key === "m1" && row.provider === "prov-a");
			expect(m1a).toMatchObject({
				engine: "hermes",
				totalRequests: 12,
				totalInputTokens: 1200,
				totalOutputTokens: 240,
				totalCacheReadTokens: 300,
				totalReasoningTokens: 25,
			});
			expect(m1a?.totalCost).toBeCloseTo(1.45, 6);
			const aux = byModel.find(row => row.key === "m2");
			expect(aux).toMatchObject({ provider: null, totalRequests: 3, includedRequests: 3 });

			expect(byProvider.map(row => row.key).sort()).toEqual(["(unattributed)", "prov-a", "prov-b"]);
			expect(byProvider.find(row => row.key === "prov-a")?.totalRequests).toBe(12);
			expect(byProvider.find(row => row.key === "(unattributed)")?.totalRequests).toBe(3);
		});
	});

	it("windows by session activity and reports the window's cumulative totals", () => {
		withFixture(home => {
			const cutoffMs = (NOW_SEC - DAY) * 1000;
			const { stats } = readHermesEngineStats(home, cutoffMs);
			// s2's usage (7 calls / $0.70) falls outside the 24h window entirely.
			expect(stats.overall.totalRequests).toBe(15);
			expect(stats.overall.totalCost).toBeCloseTo(1.5, 6);
			expect(stats.sessions).toBe(2);
			expect(stats.overall.firstTimestamp).toBe((NOW_SEC - 2 * HOUR) * 1000);
		});
	});

	it("keeps per-day series to the exact sources: sessions and messages", () => {
		withFixture(home => {
			const { stats } = readHermesEngineStats(home, null);
			const totalSessions = stats.timeSeries.reduce((sum, point) => sum + (point.sessions ?? 0), 0);
			const totalMessages = stats.timeSeries.reduce((sum, point) => sum + (point.messages ?? 0), 0);
			expect(totalSessions).toBe(3);
			expect(totalMessages).toBe(5);
			for (const point of stats.timeSeries) {
				expect(point.requests).toBeNull();
				expect(point.tokens).toBeNull();
				expect(point.cost).toBeNull();
			}
		});
	});

	it("reports metrics with no Hermes source as N/A with a note, never as zero", () => {
		withFixture(home => {
			const { stats, notes } = readHermesEngineStats(home, null);
			expect(stats.overall.errorRate).toBeNull();
			expect(stats.overall.failedRequests).toBeNull();
			expect(stats.overall.cacheSavings).toBeNull();
			expect(stats.overall.avgDuration).toBeNull();
			expect(stats.overall.avgTtft).toBeNull();
			expect(stats.overall.avgTokensPerSecond).toBeNull();
			expect(stats.overall.unpricedRequests).toBeNull();
			expect(stats.overall.totalPremiumRequests).toBeNull();
			const noted = new Set(notes.map(note => note.metric));
			for (const metric of [
				"errorRate",
				"cacheSavings",
				"avgDuration",
				"avgTtft",
				"avgTokensPerSecond",
				"windowing",
			]) {
				expect(noted.has(metric)).toBe(true);
			}
		});
	});

	it("leaves a quiescent WAL store byte-identical — no sidecar creation", () => {
		const temp = TempDir.createSync("@pi-stats-hermes-ro-");
		try {
			const home = temp.join("hermes");
			const stateDb = buildHermesFixture(home, FIXTURE, { wal: true });
			// Closing the writer checkpoints the WAL away — the store Hermes
			// leaves behind when no agent is running. Reading it must not make
			// SQLite's read protocol resurrect `-wal`/`-shm` sidecars here.
			expect(fs.existsSync(`${stateDb}-wal`)).toBe(false);
			const before = snapshotDir(home);
			readHermesEngineStats(home, null);
			readHermesEngineStats(home, (NOW_SEC - DAY) * 1000);
			expect(snapshotDir(home)).toEqual(before);
		} finally {
			temp.removeSync();
		}
	});

	it("leaves a live WAL store (sidecars present) byte-identical", () => {
		const temp = TempDir.createSync("@pi-stats-hermes-ro-");
		try {
			const home = temp.join("hermes");
			const stateDb = buildHermesFixture(home, FIXTURE, { wal: true });
			// Hold a writer connection open: the WAL sidecars stay in place,
			// mirroring the store while the Hermes engine is running.
			const holder = new Database(stateDb);
			holder.exec("PRAGMA journal_mode=WAL");
			holder.query("INSERT INTO sessions (id, source, started_at) VALUES ('hold', 'cli', ?)").run(NOW_SEC);
			try {
				const before = snapshotDir(home);
				const { stats } = readHermesEngineStats(home, null);
				expect(stats.sessions).toBe(4);
				expect(snapshotDir(home)).toEqual(before);
			} finally {
				holder.close();
			}
		} finally {
			temp.removeSync();
		}
	});

	it("aggregates older stores missing optional columns without inventing values", () => {
		const temp = TempDir.createSync("@pi-stats-hermes-legacy-");
		try {
			const home = temp.join("hermes");
			fs.mkdirSync(home, { recursive: true });
			const db = new Database(path.join(home, "state.db"));
			db.exec(`CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL)`);
			db.exec(
				`CREATE TABLE session_model_usage (
				 session_id TEXT NOT NULL, model TEXT NOT NULL,
				 api_call_count INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
				 output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
				 cache_write_tokens INTEGER NOT NULL DEFAULT 0)`,
			);
			db.query("INSERT INTO sessions (id, started_at) VALUES ('old', ?)").run(NOW_SEC - 3 * DAY);
			db.query(
				`INSERT INTO session_model_usage (session_id, model, api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
				 VALUES ('old', 'legacy-model', 4, 400, 40, 100, 0)`,
			).run();
			db.close();

			const { stats, byModel } = readHermesEngineStats(home, null);
			expect(stats.overall.totalRequests).toBe(4);
			expect(stats.overall.totalInputTokens).toBe(400);
			expect(stats.overall.totalCacheReadTokens).toBe(100);
			expect(stats.overall.cacheRate).toBeCloseTo(100 / 500, 6);
			expect(stats.overall.totalReasoningTokens).toBeNull();
			expect(stats.overall.totalCost).toBeNull();
			expect(stats.overall.includedRequests).toBeNull();
			expect(stats.overall.errorSessions).toBeNull();
			expect(stats.overall.firstTimestamp).toBe((NOW_SEC - 3 * DAY) * 1000);
			expect(byModel[0].key).toBe("legacy-model");
			// The window still applies through started_at on stores without last_activity_at.
			expect(readHermesEngineStats(home, (NOW_SEC - DAY) * 1000).stats.sessions).toBe(0);
		} finally {
			temp.removeSync();
		}
	});

	it("answers zeroed stats and a source note when no store exists", () => {
		const missing = readHermesEngineStats(null, null);
		expect(missing.stats.overall.totalRequests).toBe(0);
		expect(missing.stats.overall.totalCost).toBeNull();
		expect(missing.notes.some(note => note.metric === "source" && note.note.includes("HERMES_HOME"))).toBe(true);

		const temp = TempDir.createSync("@pi-stats-hermes-empty-");
		try {
			const absent = readHermesEngineStats(temp.join("nope"), null);
			expect(absent.stats.sessions).toBeNull();
			expect(absent.notes.some(note => note.metric === "source" && note.note.includes("no readable"))).toBe(true);
		} finally {
			temp.removeSync();
		}
	});
});

describe("combined engine roll-up", () => {
	const hermesLike: EngineAggregatedStats = {
		totalRequests: 15,
		totalInputTokens: 1500,
		totalOutputTokens: 340,
		totalCacheReadTokens: 300,
		totalCacheWriteTokens: 50,
		totalReasoningTokens: 25,
		cacheRate: 300 / 1800,
		cacheSavings: null,
		totalCost: 1.5,
		unpricedRequests: null,
		includedRequests: 3,
		totalPremiumRequests: null,
		failedRequests: null,
		errorRate: null,
		errorSessions: 1,
		avgDuration: null,
		avgTtft: null,
		avgTokensPerSecond: null,
		firstTimestamp: 1000,
		lastTimestamp: 2000,
	};
	const ompLike: EngineAggregatedStats = {
		totalRequests: 5,
		totalInputTokens: 500,
		totalOutputTokens: 100,
		totalCacheReadTokens: 100,
		totalCacheWriteTokens: 0,
		totalReasoningTokens: null,
		cacheRate: 100 / 600,
		cacheSavings: 0.5,
		totalCost: 2.5,
		unpricedRequests: 1,
		includedRequests: null,
		totalPremiumRequests: 2,
		failedRequests: 1,
		errorRate: 0.2,
		errorSessions: null,
		avgDuration: 1000,
		avgTtft: 100,
		avgTokensPerSecond: 20,
		firstTimestamp: 1500,
		lastTimestamp: 3000,
	};

	it("sums only metrics both engines report and keeps the rest N/A", () => {
		const combined = combineEngineAggregates([hermesLike, ompLike]);
		expect(combined.totalRequests).toBe(20);
		expect(combined.totalInputTokens).toBe(2000);
		expect(combined.totalCost).toBeCloseTo(4, 6);
		expect(combined.cacheRate).toBeCloseTo(400 / 2400, 6);
		// One engine lacks each of these — a combined total would fabricate coverage.
		expect(combined.totalReasoningTokens).toBeNull();
		expect(combined.cacheSavings).toBeNull();
		expect(combined.unpricedRequests).toBeNull();
		expect(combined.includedRequests).toBeNull();
		expect(combined.totalPremiumRequests).toBeNull();
		expect(combined.failedRequests).toBeNull();
		expect(combined.errorSessions).toBeNull();
		expect(combined.errorRate).toBeNull();
		expect(combined.avgDuration).toBeNull();
		// Activity window spans both engines.
		expect(combined.firstTimestamp).toBe(1000);
		expect(combined.lastTimestamp).toBe(3000);
	});
});
