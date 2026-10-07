import { describe, expect, it } from "bun:test";
import { insertMessageStats, initDb } from "@oh-my-pi/omp-stats/db";
import type { MessageStats } from "@oh-my-pi/omp-stats/types";
import { startServer, type StatsServerHandle } from "../src/server";
import { buildHermesFixture, hermesSchemaSql } from "./helpers/hermes-fixture";
import { installStatsTestIsolation } from "./helpers/temp-agent";
import { TempDir } from "@oh-my-pi/pi-utils";

installStatsTestIsolation("@pi-stats-server-engines-");

const hasForkSchema = hermesSchemaSql() !== null;

function makeOmpMessage(timestamp: number, entryId: string): MessageStats {
	return {
		sessionFile: "/tmp/engines-session.jsonl",
		entryId,
		folder: "/tmp/project",
		model: "gpt-5.4",
		provider: "openai-codex",
		api: "openai-codex-responses",
		timestamp,
		duration: 1000,
		ttft: 100,
		stopReason: "stop",
		errorMessage: null,
		usage: {
			input: 500,
			output: 100,
			cacheRead: 100,
			cacheWrite: 0,
			totalTokens: 700,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		agentType: "main",
	};
}

async function withServer(
	options: Parameters<typeof startServer>[2],
	fn: (base: string) => Promise<void>,
): Promise<void> {
	const server: StatsServerHandle = await startServer(0, "127.0.0.1", options);
	try {
		await fn(`http://127.0.0.1:${server.port}`);
	} finally {
		server.stop();
	}
}

describe.skipIf(!hasForkSchema)("dual-engine dashboard server", () => {
	it("serves both engines plus combined totals alongside the stock endpoints", async () => {
		const temp = TempDir.createSync("@pi-stats-engines-home-");
		try {
			const hermesHome = temp.join("hermes");
			const nowSec = Math.floor(Date.now() / 1000);
			buildHermesFixture(hermesHome, {
				sessions: [
					{
						id: "s1",
						startedAtSec: nowSec - 3600,
						lastActivityAtSec: nowSec - 60,
						messages: [{ role: "assistant", finishReason: "stop", timestampSec: nowSec - 60 }],
					},
				],
				usage: [
					{
						sessionId: "s1",
						model: "hermes-model",
						provider: "nous",
						apiCalls: 2,
						input: 200,
						output: 50,
						cacheRead: 0,
						cacheWrite: 0,
						cost: 0.25,
						firstSeenSec: nowSec - 3600,
						lastSeenSec: nowSec - 60,
					},
				],
			});

			await initDb();
			insertMessageStats([makeOmpMessage(Date.now(), "omp-1")]);

			await withServer({ engines: { hermesHome } }, async base => {
				const capabilities = (await (await fetch(`${base}/api/capabilities`)).json()) as { engines: boolean };
				expect(capabilities.engines).toBe(true);

				const engines = (await (await fetch(`${base}/api/engines`)).json()) as {
					engines: Array<{ label: string; overall: { totalRequests: number; totalInputTokens: number } }>;
					combined: { totalRequests: number; totalInputTokens: number };
					byModel: Array<{ label: string; key: string }>;
					byProvider: Array<{ label: string; key: string }>;
					notes: Array<{ metric: string }>;
				};
				expect(engines.engines.map(engine => engine.label)).toEqual(["Hermes", "OMP"]);
				expect(engines.engines[0].overall.totalRequests).toBe(2);
				expect(engines.engines[0].overall.totalInputTokens).toBe(200);
				expect(engines.engines[1].overall.totalRequests).toBe(1);
				expect(engines.engines[1].overall.totalInputTokens).toBe(500);
				// Combined sums the metrics both engines report.
				expect(engines.combined.totalRequests).toBe(3);
				expect(engines.combined.totalInputTokens).toBe(700);
				expect(engines.byModel.map(row => `${row.label}:${row.key}`).sort()).toEqual(
					["OMP:gpt-5.4", "Hermes:hermes-model"].sort(),
				);
				expect(engines.byProvider.some(row => row.label === "Hermes" && row.key === "nous")).toBe(true);
				expect(engines.notes.length).toBeGreaterThan(0);

				// Stock dashboard endpoints stay intact in engines mode.
				const overview = await fetch(`${base}/api/stats/overview`);
				expect(overview.status).toBe(200);
			});
		} finally {
			temp.removeSync();
		}
	});

	it("keeps the stock omp stats surface unchanged: engines endpoints stay absent", async () => {
		await initDb();
		await withServer(undefined, async base => {
			const capabilities = (await (await fetch(`${base}/api/capabilities`)).json()) as { engines: boolean };
			expect(capabilities.engines).toBe(false);
			expect((await fetch(`${base}/api/engines`)).status).toBe(404);
			expect((await fetch(`${base}/api/stats/overview`)).status).toBe(200);
		});
	});
});
