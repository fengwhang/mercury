import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as os from "node:os";
import { AuthStorage, SqliteAuthCredentialStore } from "@oh-my-pi/pi-ai/auth-storage";
import { streamSimple } from "@oh-my-pi/pi-ai/stream";
import type { Api, Model } from "@oh-my-pi/pi-ai/types";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { TempDir } from "@oh-my-pi/pi-utils";
import { __resetDirsFromEnvForTests, getAgentDbPath } from "@oh-my-pi/pi-utils/dirs";
import { Effort } from "@oh-my-pi/pi-catalog/effort";
import { type Server, YAML } from "bun";
import { beginSettingsTest, restoreSettingsTestState, type SettingsTestState } from "./helpers/settings-test-state";

const python = process.env.MERCURY_TEST_PYTHON;
const chatId = "xiaomi/mimo-v2.6-pro";
const claudeId = "anthropic/claude-test";
const expiry = Math.floor(Date.now() / 1000) + 7200;
const token = (name: string) =>
	`header.${Buffer.from(JSON.stringify({ sub: name, scope: "inference:invoke", exp: expiry })).toString("base64url")}.signature`;

describe.skipIf(!python)("Mercury Nous inference through native OMP transports", () => {
	let state: SettingsTestState;
	let temp: TempDir;
	let auth: AuthStorage;
	let server: Server<undefined>;
	let selected: string;
	let registry: ModelRegistry;
	let settings: Settings;
	let requests: Array<{ url: string; authorization: string | null; body: Record<string, unknown> }>;
	let refreshed: string;

	async function login(access: string, home = selected) {
		await Bun.write(
			`${home}/auth.json`,
			JSON.stringify({
				version: 1,
				providers: {
					nous: {
						access_token: access,
						refresh_token: "private-refresh",
						scope: "inference:invoke",
						obtained_at: "2026-10-01T00:00:00Z",
					},
				},
			}),
		);
	}

	beforeEach(async () => {
		state = beginSettingsTest();
		temp = TempDir.createSync(path.join(os.tmpdir(), "mercury-nous-native-"));
		selected = path.resolve(temp.join("hermes/profiles/private"));
		requests = [];
		refreshed = token("refreshed");
		server = Bun.serve({
			port: 0,
			hostname: "127.0.0.1",
			async fetch(request) {
				const url = new URL(request.url).pathname;
				if (url === "/v1/models")
					return Response.json({
						data: [
							{
								id: chatId,
								context_length: 128000,
								max_context_length: 256000,
								supported_parameters: ["reasoning", "tools"],
								reasoning: { supported_efforts: ["low", "high"] },
							},
							{ id: claudeId, context_length: 200000, supported_parameters: [] },
						],
					});
				if (url === "/api/oauth/token")
					return Response.json({
						access_token: refreshed,
						refresh_token: "rotated-refresh",
						scope: "inference:invoke",
						expires_in: 7200,
					});
				const body = (await request.json()) as Record<string, unknown>;
				requests.push({ url, body, authorization: request.headers.get("authorization") });
				let data: string;
				if (url === "/v1/chat/completions") {
					const chunk = (delta: unknown, finish_reason: string | null) =>
						JSON.stringify({
							id: "test",
							object: "chat.completion.chunk",
							created: 0,
							model: chatId,
							choices: [{ index: 0, delta, finish_reason }],
						});
					data = `data: ${chunk({ role: "assistant", content: "ok" }, null)}\n\ndata: ${chunk({}, "stop")}\n\ndata: [DONE]\n\n`;
				} else if (url === "/v1/messages") {
					const event = (type: string, fields: Record<string, unknown>) =>
						`event: ${type}\ndata: ${JSON.stringify({ type, ...fields })}\n\n`;
					data =
						event("message_start", {
							message: {
								id: "test",
								type: "message",
								role: "assistant",
								content: [],
								model: claudeId,
								stop_reason: null,
								stop_sequence: null,
								usage: { input_tokens: 1, output_tokens: 0 },
							},
						}) +
						event("content_block_start", { index: 0, content_block: { type: "text", text: "" } }) +
						event("content_block_delta", { index: 0, delta: { type: "text_delta", text: "ok" } }) +
						event("content_block_stop", { index: 0 }) +
						event("message_delta", {
							delta: { stop_reason: "end_turn", stop_sequence: null },
							usage: { output_tokens: 1 },
						}) +
						event("message_stop", {});
				} else return new Response("unexpected endpoint", { status: 404 });
				return new Response(data, { headers: { "content-type": "text/event-stream" } });
			},
		});
		const base = `http://127.0.0.1:${server.port}`;
		Object.assign(process.env, {
			MERCURY_HOME: path.resolve(temp.path()),
			HERMES_HOME: selected,
			MERCURY_REPO: new URL("../../../..", import.meta.url).pathname,
			MERCURY_PYTHON: python!,
			MERCURY_CONFIG: `${selected}/config.yaml`,
			PI_CODING_AGENT_DIR: path.resolve(temp.join("omp")),
			HERMES_SHARED_AUTH_DIR: path.resolve(temp.join("shared")),
			NOUS_INFERENCE_BASE_URL: `${base}/v1`,
			NOUS_PORTAL_BASE_URL: base,
		});
		delete process.env.OMP_PROFILE;
		delete process.env.PI_PROFILE;
		__resetDirsFromEnvForTests();
		await login(token("private"));
		await login(token("default"), temp.join("hermes"));
		await Bun.write(
			process.env.MERCURY_CONFIG!,
			YAML.stringify({
				models: {
					default: "openai-codex/gpt-6.1-sol",
					delegate_model: `nous/${chatId}`,
					delegate_fallback: `nous/${claudeId}`,
					reasoning_overrides: { [`nous/${chatId}`]: "high" },
					context_windows: { [`nous/${chatId}`]: 256000 },
				},
				hermes: {},
				omp: {},
			}),
		);
		auth = await AuthStorage.create(getAgentDbPath(), { usageProviderResolver: () => undefined });
		settings = await Settings.init({ agentDir: `${selected}/omp`, cwd: temp.path() });
		registry = new ModelRegistry(auth, temp.join("models.yml"), { settings });
		await registry.hydrateCredentialScopedModelCaches();
	}, 30000);

	afterEach(async () => {
		auth?.close();
		await server?.stop(true);
		restoreSettingsTestState(state);
		__resetDirsFromEnvForTests();
		temp.removeSync();
	}, 30000);

	async function infer(model: Model<Api>, reasoning?: Effort) {
		const result = await streamSimple(
			model,
			{ messages: [{ role: "user", content: "hello", timestamp: 0 }] },
			{
				apiKey: registry.resolver(model),
				reasoning,
				maxTokens: 64,
			},
		).result();
		expect(result.errorMessage).toBeUndefined();
		expect(result.content).toEqual([{ type: "text", text: "ok" }]);
	}

	test("shared fallback resolves Nous and both transports use profile-local rotating credentials", async () => {
		const chat = registry.find("nous", chatId)!;
		const claude = registry.find("nous", claudeId)!;
		expect(chat).toBeDefined();
		expect(claude).toBeDefined();
		expect(chat.thinking?.efforts).toEqual([Effort.Low, Effort.High]);
		expect(chat.contextWindow).toBe(256000);
		expect(chat.maxContextWindow).toBe(256000);
		expect(settings.get("retry.fallbackChains")).toEqual({ [`nous/${chatId}`]: [`nous/${claudeId}`] });
		registry.syncExtensionSources([]);
		expect(registry.find("nous", chatId)).toBeDefined();
		await infer(chat, Effort.High);
		expect(requests.at(-1)).toMatchObject({
			url: "/v1/chat/completions",
			authorization: `Bearer ${token("private")}`,
			body: { model: chatId, reasoning: { effort: "high" } },
		});
		const rotated = token("peer-rotated");
		await login(rotated);
		await infer(claude);
		expect(requests.at(-1)).toMatchObject({
			url: "/v1/messages",
			authorization: `Bearer ${rotated}`,
			body: { model: claudeId },
		});
		expect(claude.isOAuth).not.toBe(true);
		await infer(chat, Effort.Low);
		expect(requests.at(-1)?.authorization).toBe(`Bearer ${rotated}`);
		await auth.getApiKey("nous", undefined, { baseUrl: chat.baseUrl, forceRefresh: true });
		await infer(chat);
		expect(requests.at(-1)?.authorization).toBe(`Bearer ${refreshed}`);
		const store = await SqliteAuthCredentialStore.open(getAgentDbPath());
		expect(store.listAuthCredentials("nous")).toEqual([]);
		store.close();
		expect(await Bun.file(process.env.MERCURY_CONFIG!).text()).not.toContain(rotated);
	}, 30000);

	test("runtime keys win and logout cannot reuse stale keys or another profile's login", async () => {
		const model = registry.find("nous", chatId)!;
		auth.setRuntimeApiKey("nous", "explicit-test-key");
		await infer(model);
		expect(requests.at(-1)?.authorization).toBe("Bearer explicit-test-key");
		auth.removeRuntimeApiKey("nous");
		await fs.rm(`${selected}/auth.json`);
		await fs.rm(temp.join("shared"), { recursive: true, force: true });
		for (let i = 0; i < 2; i++) await expect(registry.getApiKey(model)).rejects.toThrow("login is unavailable");
		expect(requests).toHaveLength(1);
	}, 30000);
});
