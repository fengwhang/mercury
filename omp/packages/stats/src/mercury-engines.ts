/**
 * MERCURY-OMP PATCH (dual-engine stats): Hermes engine metrics for `mercury stats`.
 *
 * The Mercury distribution runs two engines over one state tree: the OMP
 * coding engine (this dashboard's stock data) and the Hermes personal agent.
 * This module reads Hermes' numbers from its session store — read-only, never
 * mutating live state — and shapes them into the dashboard's metric language
 * so both engines read as one product.
 *
 * Hermes source of truth (state.db, schema in hermes/mercury_state_common.py):
 *   - `session_model_usage` — per (session, model, billing_provider,
 *     billing_mode, task) usage ledger: api_call_count, token buckets,
 *     estimated/actual cost, cost_status, first_seen/last_seen. Cumulative
 *     per row; aux LLM calls (title_generation, compression, …) arrive with a
 *     non-empty `task`.
 *   - `sessions` — session rows: started_at/last_activity_at (unix seconds),
 *     tool_call_count, model/billing columns.
 *   - `messages` — per-turn rows; the last message's `finish_reason` classifies
 *     session failure (error / agent_error / content_filter).
 *
 * Metrics with no Hermes source (per-call latency/TTFT, per-call error rate,
 * cache savings, per-day usage for cumulative rows) are reported as `null`
 * with an explanatory note — never invented.
 */

import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { getDashboardStats, getTimeRangeConfig } from "./aggregator";
import { getStatsByProvider, initDb } from "./db";
import type { ProviderAggregate } from "./shared-types";
import type {
	EngineAggregatedStats,
	EngineBreakdownRow,
	EngineMetricNote,
	EngineStats,
	EngineTimeSeriesPoint,
	EnginesDashboardStats,
} from "./shared-types";

/** Hermes session classification for failed turns (mercury_state_common.py). */
const HERMES_ERROR_FINISH_REASONS = ["error", "agent_error", "content_filter"];

/**
 * The absent-source contract: every metric Hermes or OMP cannot report stays
 * `null` (rendered N/A) rather than defaulting to zero.
 */
const EMPTY_ENGINE_OVERALL: EngineAggregatedStats = {
	totalRequests: 0,
	totalInputTokens: 0,
	totalOutputTokens: 0,
	totalCacheReadTokens: 0,
	totalCacheWriteTokens: 0,
	totalReasoningTokens: null,
	cacheRate: null,
	cacheSavings: null,
	totalCost: null,
	unpricedRequests: null,
	includedRequests: null,
	totalPremiumRequests: null,
	failedRequests: null,
	errorRate: null,
	errorSessions: null,
	avgDuration: null,
	avgTtft: null,
	avgTokensPerSecond: null,
	firstTimestamp: null,
	lastTimestamp: null,
};

interface UsageTotals {
	requests: number;
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	reasoning: number | null;
	cost: number | null;
	includedRequests: number | null;
	first: number | null;
	last: number | null;
}

interface UsageOverallRow {
	requests: number | null;
	input: number | null;
	output: number | null;
	cache_read: number | null;
	cache_write: number | null;
	reasoning: number | null;
	cost: number | null;
	included: number | null;
	first_ts: number | null;
	last_ts: number | null;
}

interface UsageGroupRow extends UsageOverallRow {
	model: string;
	provider: string | null;
}

interface CountRow {
	n: number;
}

interface TimestampRow {
	ts: number;
}

export interface HermesReadResult {
	stats: EngineStats;
	byModel: EngineBreakdownRow[];
	byProvider: EngineBreakdownRow[];
	notes: EngineMetricNote[];
}

/**
 * Resolve the Hermes home to read: `HERMES_HOME` (set by bin/mercury and
 * profile env), else `$MERCURY_HOME/hermes`. Null outside a Mercury launch.
 */
export function resolveHermesHome(): string | null {
	const explicit = process.env.HERMES_HOME?.trim();
	if (explicit) return explicit;
	const mercuryHome = process.env.MERCURY_HOME?.trim();
	if (mercuryHome) return path.join(mercuryHome, "hermes");
	return null;
}

/** Prompt input served from cache: cache reads / (uncached input + cache reads). */
function cacheRateOf(inputTokens: number, cacheReadTokens: number): number | null {
	const denominator = inputTokens + cacheReadTokens;
	return denominator > 0 ? cacheReadTokens / denominator : null;
}

/** Local-day bucket start, matching how the client labels chart points. */
function localDayStart(ms: number): number {
	const date = new Date(ms);
	date.setHours(0, 0, 0, 0);
	return date.getTime();
}

interface HermesStoreHandle {
	db: Database;
	cleanup: () => void;
}

/**
 * Open the Hermes store without ever touching live state.
 *
 * Rollback-journal stores open read-only in place — proven zero-touch.
 * WAL-mode stores are read through a byte copy in a temp dir: SQLite's WAL
 * read protocol creates the `-wal`/`-shm` sidecars and writes reader marks
 * into `-shm` even from a read-only connection, so in-place reads cannot
 * promise byte-identical live state. The copy includes a present `-wal` so
 * un-checkpointed commits are seen; a torn tail frame is dropped by the
 * copy's WAL recovery (it was not yet a committed snapshot).
 */
function openHermesStoreReadOnly(stateDb: string): HermesStoreHandle | null {
	let header: Buffer;
	try {
		const fd = fs.openSync(stateDb, "r");
		try {
			header = Buffer.alloc(20);
			fs.readSync(fd, header, 0, 20, 0);
		} finally {
			fs.closeSync(fd);
		}
	} catch {
		return null;
	}
	const isWalMode = header[18] === 2;
	if (!isWalMode) {
		try {
			const db = new Database(stateDb, { readonly: true });
			return { db, cleanup: () => db.close() };
		} catch {
			return null;
		}
	}

	let tmpDir: string;
	try {
		tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mercury-stats-hermes-"));
		fs.copyFileSync(stateDb, path.join(tmpDir, "state.db"));
		if (fs.existsSync(`${stateDb}-wal`)) {
			fs.copyFileSync(`${stateDb}-wal`, path.join(tmpDir, "state.db-wal"));
		}
	} catch {
		return null;
	}
	try {
		// Normal open on the copy: WAL recovery may write — but only to the copy.
		const db = new Database(path.join(tmpDir, "state.db"));
		return {
			db,
			cleanup: () => {
				db.close();
				fs.rmSync(tmpDir, { recursive: true, force: true });
			},
		};
	} catch {
		fs.rmSync(tmpDir, { recursive: true, force: true });
		return null;
	}
}

/**
 * Read Hermes engine aggregates from a Hermes home. Read-only through
 * {@link openHermesStoreReadOnly}: live state is never written or touched.
 *
 * Usage rows are cumulative per (session, model, provider, mode, task), so a
 * time-range window selects sessions by last activity and reports their full
 * cumulative totals — per-request windowing is not derivable from Hermes data.
 */
export function readHermesEngineStats(hermesHome: string | null, cutoffMs: number | null): HermesReadResult {
	const notes: EngineMetricNote[] = [
		{
			engine: "hermes",
			metric: "windowing",
			note: "Hermes usage rows are cumulative per session; range windows select sessions by last activity and report their full totals",
		},
		{
			engine: "hermes",
			metric: "cacheSavings",
			note: "no Hermes equivalent — dollar savings need price-rate tables Hermes does not store",
		},
		{
			engine: "hermes",
			metric: "errorRate",
			note: "no Hermes equivalent — usage rows are cumulative, not per-call; failed sessions are reported instead",
		},
		{
			engine: "hermes",
			metric: "avgDuration",
			note: "no Hermes equivalent — latency is only logged, never persisted",
		},
		{
			engine: "hermes",
			metric: "avgTtft",
			note: "no Hermes equivalent — time-to-first-token is never persisted",
		},
		{
			engine: "hermes",
			metric: "avgTokensPerSecond",
			note: "no Hermes equivalent — needs per-call durations",
		},
		{
			engine: "hermes",
			metric: "unpricedRequests",
			note: "no Hermes equivalent — Hermes prices every route or marks it subscription-included",
		},
		{
			engine: "hermes",
			metric: "totalPremiumRequests",
			note: "no Hermes equivalent",
		},
		{
			engine: "hermes",
			metric: "timeSeries.tokens",
			note: "no Hermes equivalent — cumulative per-session usage cannot be split into per-day buckets; sessions and messages per day are exact",
		},
	];

	const stateDb = hermesHome !== null ? path.join(hermesHome, "state.db") : null;
	const store = stateDb !== null ? openHermesStoreReadOnly(stateDb) : null;
	if (store === null) {
		notes.push({
			engine: "hermes",
			metric: "source",
			note:
				stateDb === null
					? "no Hermes home resolved (HERMES_HOME / MERCURY_HOME unset)"
					: `no readable Hermes session store at ${stateDb}`,
		});
		return {
			stats: {
				engine: "hermes",
				label: "Hermes",
				sessions: null,
				overall: { ...EMPTY_ENGINE_OVERALL },
				timeSeries: [],
			},
			byModel: [],
			byProvider: [],
			notes,
		};
	}
	const db = store.db;

	try {
		const cutoffSec = cutoffMs !== null ? cutoffMs / 1000 : null;
		const usageCols = new Set(
			(db.query(`PRAGMA table_info("session_model_usage")`).all() as Array<{ name: string }>).map(row => row.name),
		);
		const sessionCols = new Set(
			(db.query(`PRAGMA table_info("sessions")`).all() as Array<{ name: string }>).map(row => row.name),
		);
		const messageCols = new Set(
			(db.query(`PRAGMA table_info("messages")`).all() as Array<{ name: string }>).map(row => row.name),
		);
		const hasUsage = usageCols.size > 0;
		const hasSessions = sessionCols.size > 0;
		const hasMessages = messageCols.size > 0;

		if (!hasUsage) {
			for (const metric of ["totalRequests", "totalCost", "byModel", "byProvider"]) {
				notes.push({
					engine: "hermes",
					metric,
					note: "source table `session_model_usage` is missing from the Hermes session store",
				});
			}
		}

		// Session window predicate, in seconds (Hermes timestamps are unix seconds).
		// `started_at` is the one required session column; later schema revisions
		// added `last_activity_at` and older stores lack it.
		const sessionActivityExpr = (table: string) =>
			sessionCols.has("last_activity_at")
				? `COALESCE(${table}.last_activity_at, ${table}.started_at)`
				: `${table}.started_at`;
		const windowSql = (table: string) =>
			cutoffSec !== null ? `${sessionActivityExpr(table)} >= ${cutoffSec}` : "1=1";

		const reasoningExpr = usageCols.has("reasoning_tokens") ? "COALESCE(SUM(u.reasoning_tokens), 0)" : "NULL";
		const costExpr = usageCols.has("estimated_cost_usd") ? "COALESCE(SUM(u.estimated_cost_usd), 0)" : "NULL";
		const includedExpr = usageCols.has("cost_status")
			? "COALESCE(SUM(CASE WHEN u.cost_status = 'included' THEN u.api_call_count ELSE 0 END), 0)"
			: "NULL";
		const firstExpr = usageCols.has("first_seen")
			? "MIN(COALESCE(u.first_seen, s.started_at)) * 1000"
			: "MIN(s.started_at) * 1000";
		const lastExpr = usageCols.has("last_seen")
			? `MAX(COALESCE(u.last_seen, ${sessionActivityExpr("s")})) * 1000`
			: `MAX(${sessionActivityExpr("s")}) * 1000`;

		let totals: UsageTotals = {
			requests: 0,
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			reasoning: null,
			cost: null,
			includedRequests: null,
			first: null,
			last: null,
		};
		let byModel: EngineBreakdownRow[] = [];
		let byProvider: EngineBreakdownRow[] = [];

		if (hasUsage && hasSessions) {
			const overallRow = db
				.query(
					`SELECT COALESCE(SUM(u.api_call_count), 0) AS requests,
					        COALESCE(SUM(u.input_tokens), 0) AS input,
					        COALESCE(SUM(u.output_tokens), 0) AS output,
					        COALESCE(SUM(u.cache_read_tokens), 0) AS cache_read,
					        COALESCE(SUM(u.cache_write_tokens), 0) AS cache_write,
					        ${reasoningExpr} AS reasoning,
					        ${costExpr} AS cost,
					        ${includedExpr} AS included,
					        ${firstExpr} AS first_ts,
					        ${lastExpr} AS last_ts
					 FROM session_model_usage u JOIN sessions s ON s.id = u.session_id
					 WHERE ${windowSql("s")}`,
				)
				.get() as UsageOverallRow;
			totals = {
				requests: overallRow.requests ?? 0,
				input: overallRow.input ?? 0,
				output: overallRow.output ?? 0,
				cacheRead: overallRow.cache_read ?? 0,
				cacheWrite: overallRow.cache_write ?? 0,
				reasoning: overallRow.reasoning,
				cost: overallRow.cost,
				includedRequests: overallRow.included,
				first: overallRow.first_ts,
				last: overallRow.last_ts,
			};

			const providerExpr = usageCols.has("billing_provider") ? "u.billing_provider" : "''";
			const groupRows = db
				.query(
					`SELECT u.model AS model, ${providerExpr} AS provider,
					        COALESCE(SUM(u.api_call_count), 0) AS requests,
					        COALESCE(SUM(u.input_tokens), 0) AS input,
					        COALESCE(SUM(u.output_tokens), 0) AS output,
					        COALESCE(SUM(u.cache_read_tokens), 0) AS cache_read,
					        COALESCE(SUM(u.cache_write_tokens), 0) AS cache_write,
					        ${reasoningExpr} AS reasoning,
					        ${costExpr} AS cost,
					        ${includedExpr} AS included,
					        ${firstExpr} AS first_ts,
					        ${lastExpr} AS last_ts
					 FROM session_model_usage u JOIN sessions s ON s.id = u.session_id
					 WHERE ${windowSql("s")}
					 GROUP BY u.model, ${providerExpr}`,
				)
				.all() as UsageGroupRow[];

			const toBreakdown = (row: UsageGroupRow, key: string, provider: string | null): EngineBreakdownRow => ({
				engine: "hermes",
				label: "Hermes",
				key,
				provider,
				totalRequests: row.requests ?? 0,
				totalInputTokens: row.input ?? 0,
				totalOutputTokens: row.output ?? 0,
				totalCacheReadTokens: row.cache_read ?? 0,
				totalCacheWriteTokens: row.cache_write ?? 0,
				totalReasoningTokens: row.reasoning,
				totalCost: row.cost,
				unpricedRequests: null,
				includedRequests: row.included,
				totalPremiumRequests: null,
				firstTimestamp: row.first_ts,
				lastTimestamp: row.last_ts,
			});

			byModel = groupRows.map(row =>
				toBreakdown(row, row.model, row.provider === null || row.provider === "" ? null : row.provider),
			);

			const providerGroups = new Map<string, EngineBreakdownRow>();
			for (const row of groupRows) {
				const providerName = row.provider ?? "";
				const key = providerName === "" ? "(unattributed)" : providerName;
				const existing = providerGroups.get(key);
				if (!existing) {
					providerGroups.set(key, toBreakdown(row, key, providerName === "" ? null : providerName));
					continue;
				}
				existing.totalRequests += row.requests ?? 0;
				existing.totalInputTokens += row.input ?? 0;
				existing.totalOutputTokens += row.output ?? 0;
				existing.totalCacheReadTokens += row.cache_read ?? 0;
				existing.totalCacheWriteTokens += row.cache_write ?? 0;
				if (row.reasoning !== null && existing.totalReasoningTokens !== null) {
					existing.totalReasoningTokens += row.reasoning;
				}
				if (row.cost !== null && existing.totalCost !== null) existing.totalCost += row.cost;
				if (row.included !== null && existing.includedRequests !== null) {
					existing.includedRequests += row.included;
				}
				if (row.first_ts !== null) {
					existing.firstTimestamp =
						existing.firstTimestamp === null ? row.first_ts : Math.min(existing.firstTimestamp, row.first_ts);
				}
				if (row.last_ts !== null) {
					existing.lastTimestamp =
						existing.lastTimestamp === null ? row.last_ts : Math.max(existing.lastTimestamp, row.last_ts);
				}
			}
			byProvider = [...providerGroups.values()].sort((a, b) => b.totalRequests - a.totalRequests);
			byModel.sort((a, b) => b.totalRequests - a.totalRequests);
		}

		let sessions: number | null = null;
		let errorSessions: number | null = null;
		if (hasSessions) {
			const sessionsRow = db.query(`SELECT COUNT(*) AS n FROM sessions s WHERE ${windowSql("s")}`).get() as CountRow;
			sessions = sessionsRow.n;
			if (hasMessages && messageCols.has("finish_reason") && messageCols.has("id")) {
				const reasons = HERMES_ERROR_FINISH_REASONS.map(reason => `'${reason}'`).join(", ");
				const errorRow = db
					.query(
						`SELECT COUNT(*) AS n FROM sessions s
						 WHERE ${windowSql("s")} AND EXISTS (
						   SELECT 1 FROM messages m
						   WHERE m.session_id = s.id
						     AND m.id = (SELECT MAX(m2.id) FROM messages m2 WHERE m2.session_id = s.id)
						     AND lower(COALESCE(m.finish_reason, '')) IN (${reasons})
						 )`,
					)
					.get() as CountRow;
				errorSessions = errorRow.n;
			} else {
				notes.push({
					engine: "hermes",
					metric: "errorSessions",
					note: "source table `messages` is missing from the Hermes session store",
				});
			}
		}

		const seriesBuckets = new Map<number, EngineTimeSeriesPoint>();
		const bucketAt = (dayMs: number): EngineTimeSeriesPoint => {
			const existing = seriesBuckets.get(dayMs);
			if (existing) return existing;
			const point: EngineTimeSeriesPoint = {
				timestamp: dayMs,
				sessions: hasSessions && sessionCols.has("started_at") ? 0 : null,
				messages: hasMessages && messageCols.has("timestamp") ? 0 : null,
				requests: null,
				errors: null,
				tokens: null,
				cost: null,
			};
			seriesBuckets.set(dayMs, point);
			return point;
		};
		const seriesCutoffSec = cutoffSec ?? 0;
		if (hasSessions && sessionCols.has("started_at")) {
			const rows = db
				.query(`SELECT started_at * 1000 AS ts FROM sessions WHERE started_at >= ${seriesCutoffSec}`)
				.all() as TimestampRow[];
			for (const row of rows) {
				const point = bucketAt(localDayStart(row.ts));
				point.sessions = (point.sessions ?? 0) + 1;
			}
		}
		if (hasMessages && messageCols.has("timestamp")) {
			const rows = db
				.query(`SELECT timestamp * 1000 AS ts FROM messages WHERE timestamp >= ${seriesCutoffSec}`)
				.all() as TimestampRow[];
			for (const row of rows) {
				const point = bucketAt(localDayStart(row.ts));
				point.messages = (point.messages ?? 0) + 1;
			}
		}
		const timeSeries = [...seriesBuckets.values()].sort((a, b) => a.timestamp - b.timestamp);

		const overall: EngineAggregatedStats = {
			totalRequests: totals.requests,
			totalInputTokens: totals.input,
			totalOutputTokens: totals.output,
			totalCacheReadTokens: totals.cacheRead,
			totalCacheWriteTokens: totals.cacheWrite,
			totalReasoningTokens: totals.reasoning,
			cacheRate: cacheRateOf(totals.input, totals.cacheRead),
			cacheSavings: null,
			totalCost: totals.cost,
			unpricedRequests: null,
			includedRequests: totals.includedRequests,
			totalPremiumRequests: null,
			failedRequests: null,
			errorRate: null,
			errorSessions,
			avgDuration: null,
			avgTtft: null,
			avgTokensPerSecond: null,
			firstTimestamp: totals.first,
			lastTimestamp: totals.last,
		};

		return {
			stats: { engine: "hermes", label: "Hermes", sessions, overall, timeSeries },
			byModel,
			byProvider,
			notes,
		};
	} finally {
		store.cleanup();
	}
}

/**
 * Roll engine aggregates into a combined total. Only metrics BOTH engines
 * genuinely report are summed; anything one engine lacks stays `null` so the
 * combined card never fabricates coverage. Timestamps span both engines.
 */
export function combineEngineAggregates(engines: EngineAggregatedStats[]): EngineAggregatedStats {
	const combined: EngineAggregatedStats = { ...EMPTY_ENGINE_OVERALL };
	const sum = (
		key:
			| "totalRequests"
			| "totalInputTokens"
			| "totalOutputTokens"
			| "totalCacheReadTokens"
			| "totalCacheWriteTokens",
	) => {
		combined[key] = engines.reduce((total, engine) => total + engine[key], 0);
	};
	sum("totalRequests");
	sum("totalInputTokens");
	sum("totalOutputTokens");
	sum("totalCacheReadTokens");
	sum("totalCacheWriteTokens");

	const sumIfBoth = (
		key:
			| "totalReasoningTokens"
			| "totalCost"
			| "unpricedRequests"
			| "includedRequests"
			| "totalPremiumRequests"
			| "failedRequests"
			| "errorSessions",
	) => {
		const values = engines.map(engine => engine[key]).filter((value): value is number => value !== null);
		combined[key] = values.length === engines.length ? values.reduce((total, value) => total + value, 0) : null;
	};
	sumIfBoth("totalReasoningTokens");
	sumIfBoth("totalCost");
	sumIfBoth("unpricedRequests");
	sumIfBoth("includedRequests");
	sumIfBoth("totalPremiumRequests");
	sumIfBoth("failedRequests");
	sumIfBoth("errorSessions");

	combined.cacheRate = cacheRateOf(combined.totalInputTokens, combined.totalCacheReadTokens);

	const firsts = engines.map(engine => engine.firstTimestamp).filter((value): value is number => value !== null);
	const lasts = engines.map(engine => engine.lastTimestamp).filter((value): value is number => value !== null);
	combined.firstTimestamp = firsts.length > 0 ? Math.min(...firsts) : null;
	combined.lastTimestamp = lasts.length > 0 ? Math.max(...lasts) : null;
	return combined;
}

/**
 * The `GET /api/engines` payload: Hermes and OMP side by side in the
 * dashboard's metric language, plus combined totals and honest coverage notes.
 */
export async function getEnginesDashboardStats(
	range?: string | null,
	hermesHome?: string | null,
): Promise<EnginesDashboardStats> {
	const { cutoff } = getTimeRangeConfig(range);
	const resolvedHome = hermesHome === undefined ? resolveHermesHome() : hermesHome;
	const hermes = readHermesEngineStats(resolvedHome, cutoff);

	const ompDashboard = await getDashboardStats(range);
	await initDb();
	const ompProviders = getStatsByProvider(cutoff);

	const ompOverall: EngineAggregatedStats = {
		totalRequests: ompDashboard.overall.totalRequests,
		totalInputTokens: ompDashboard.overall.totalInputTokens,
		totalOutputTokens: ompDashboard.overall.totalOutputTokens,
		totalCacheReadTokens: ompDashboard.overall.totalCacheReadTokens,
		totalCacheWriteTokens: ompDashboard.overall.totalCacheWriteTokens,
		totalReasoningTokens: null,
		cacheRate: ompDashboard.overall.cacheRate,
		cacheSavings: ompDashboard.overall.cacheSavings,
		totalCost: ompDashboard.overall.totalCost,
		unpricedRequests: ompDashboard.overall.unpricedRequests,
		includedRequests: null,
		totalPremiumRequests: ompDashboard.overall.totalPremiumRequests,
		failedRequests: ompDashboard.overall.failedRequests,
		errorRate: ompDashboard.overall.errorRate,
		errorSessions: null,
		avgDuration: ompDashboard.overall.avgDuration,
		avgTtft: ompDashboard.overall.avgTtft,
		avgTokensPerSecond: ompDashboard.overall.avgTokensPerSecond,
		firstTimestamp: ompDashboard.overall.firstTimestamp || null,
		lastTimestamp: ompDashboard.overall.lastTimestamp || null,
	};

	const ompByModel: EngineBreakdownRow[] = ompDashboard.byModel.map(row => ({
		engine: "omp",
		label: "OMP",
		key: row.model,
		provider: row.provider,
		totalRequests: row.totalRequests,
		totalInputTokens: row.totalInputTokens,
		totalOutputTokens: row.totalOutputTokens,
		totalCacheReadTokens: row.totalCacheReadTokens,
		totalCacheWriteTokens: row.totalCacheWriteTokens,
		totalReasoningTokens: null,
		totalCost: row.totalCost,
		unpricedRequests: row.unpricedRequests,
		includedRequests: null,
		totalPremiumRequests: row.totalPremiumRequests,
		firstTimestamp: row.firstTimestamp || null,
		lastTimestamp: row.lastTimestamp || null,
	}));

	const ompByProvider: EngineBreakdownRow[] = ompProviders.map((row: ProviderAggregate) => ({
		engine: "omp",
		label: "OMP",
		key: row.provider,
		provider: row.provider,
		totalRequests: row.totalRequests,
		totalInputTokens: row.totalInputTokens,
		totalOutputTokens: row.totalOutputTokens,
		totalCacheReadTokens: row.totalCacheReadTokens,
		totalCacheWriteTokens: row.totalCacheWriteTokens,
		totalReasoningTokens: null,
		totalCost: row.totalCost,
		unpricedRequests: row.unpricedRequests,
		includedRequests: null,
		totalPremiumRequests: row.totalPremiumRequests,
		firstTimestamp: null,
		lastTimestamp: null,
	}));

	const ompSeries: EngineTimeSeriesPoint[] = ompDashboard.timeSeries.map(point => ({
		timestamp: point.timestamp,
		sessions: null,
		messages: null,
		requests: point.requests,
		errors: point.errors,
		tokens: point.tokens,
		cost: point.cost,
	}));

	const engines: EngineStats[] = [
		hermes.stats,
		{ engine: "omp", label: "OMP", sessions: null, overall: ompOverall, timeSeries: ompSeries },
	];

	const ompNotes: EngineMetricNote[] = [
		{
			engine: "omp",
			metric: "totalReasoningTokens",
			note: "no OMP equivalent — reasoning tokens are folded into output tokens",
		},
		{
			engine: "omp",
			metric: "includedRequests",
			note: "no OMP equivalent — subscription usage is priced as API-equivalent estimates",
		},
		{
			engine: "omp",
			metric: "errorSessions",
			note: "no OMP equivalent — OMP reports per-request errors and error rate",
		},
		{
			engine: "omp",
			metric: "sessions",
			note: "no aggregate equivalent — OMP sessions are browsed in the Traces and Requests views",
		},
		{
			engine: "omp",
			metric: "timeSeries.sessions",
			note: "no OMP equivalent — daily buckets carry requests, errors, tokens and cost instead",
		},
	];

	return {
		range: range ?? "24h",
		cutoff,
		engines,
		combined: combineEngineAggregates(engines.map(engine => engine.overall)),
		byModel: [...hermes.byModel, ...ompByModel],
		byProvider: [...hermes.byProvider, ...ompByProvider],
		notes: [
			...hermes.notes,
			...ompNotes,
			{
				engine: "omp",
				metric: "combined",
				note: "combined totals only sum metrics both engines report; anything one engine lacks stays N/A",
			},
		],
	};
}
