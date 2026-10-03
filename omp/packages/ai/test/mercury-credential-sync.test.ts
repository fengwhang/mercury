import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { __resetDirsFromEnvForTests, getAgentDbPath } from "@oh-my-pi/pi-utils/dirs";
import { AuthStorage, SqliteAuthCredentialStore } from "../src/auth-storage";
import { mercuryCredentialStore, type MercuryCredentialContext } from "../src/auth/mercury-store";
import { getProviderDefinition } from "../src/registry";

const python = process.env.MERCURY_TEST_PYTHON;
describe.skipIf(!python)("Mercury cross-engine credential IPC", () => {
	let dir: string;
	let context: MercuryCredentialContext;
	let auth: AuthStorage | undefined;
	const spies: Array<{ mockRestore(): void }> = [];
	const token = (workspace: string) =>
		`header.${Buffer.from(
			JSON.stringify({
				exp: Math.floor(Date.now() / 1000) + 3600,
				"https://api.openai.com/auth": { chatgpt_account_id: workspace, chatgpt_plan_type: "pro" },
				"https://api.openai.com/profile": { email: "person@example.test" },
			}),
		).toString("base64url")}.signature`;
	beforeEach(() => {
		dir = fs.mkdtempSync(path.join(os.tmpdir(), "mercury-provider-ipc-"));
		context = {
			home: dir,
			hermesHome: path.join(dir, "hermes"),
			repo: path.resolve(import.meta.dir, "../../../.."),
			python: python!,
		};
		fs.mkdirSync(context.hermesHome);
	});
	afterEach(() => {
		for (const spy of spies.splice(0)) spy.mockRestore();
		auth?.close();
		auth = undefined;
		fs.rmSync(dir, { recursive: true, force: true });
	});
	async function open() {
		const native = await SqliteAuthCredentialStore.open(path.join(dir, "agent.db"));
		const store = mercuryCredentialStore(native, context);
		auth = new AuthStorage(store, { usageProviderResolver: () => undefined });
		await auth.reload();
		return { storage: auth, store };
	}
	test("Hermes setup login yields a usable OMP Codex token and workspace", async () => {
		const access = token("personal");
		await Bun.write(
			path.join(context.hermesHome, "auth.json"),
			JSON.stringify({
				version: 1,
				providers: { "openai-codex": { tokens: { access_token: access, refresh_token: "fake-refresh" } } },
			}),
		);
		const { storage, store } = await open();
		expect(await storage.getApiKey("openai-codex")).toBe(access);
		const credential = store.listAuthCredentials("openai-codex")[0]?.credential;
		expect(credential?.type === "oauth" ? credential.accountId : undefined).toBe("personal");
	});
	test("direct OMP login preserves independent subscriptions of the same person", async () => {
		const { storage, store } = await open();
		const definition = getProviderDefinition("openai-codex")!;
		const login = spyOn(definition, "login");
		spies.push(login);
		for (const workspace of ["personal", "team"]) {
			login.mockResolvedValueOnce({
				access: token(workspace),
				refresh: `fake-${workspace}`,
				expires: Date.now() + 3600_000,
				accountId: workspace,
				orgId: workspace,
				email: "person@example.test",
				orgName: workspace,
			});
			await storage.login("openai-codex", { onAuth: () => {}, onPrompt: async () => "" });
		}
		const canonical = await Bun.file(path.join(context.hermesHome, "auth.json")).json();
		expect(
			canonical.credential_pool["openai-codex"]
				.map((entry: { refresh_token: string }) => entry.refresh_token)
				.sort(),
		).toEqual(["fake-personal", "fake-team"]);
		expect(
			store
				.listAuthCredentials("openai-codex")
				.map(entry => (entry.credential.type === "oauth" ? entry.credential.orgId : ""))
				.sort(),
		).toEqual(["personal", "team"]);
	});
	test("OMP API-key login syncs to Hermes and logout survives a process restart", async () => {
		const { storage } = await open();
		await storage.set("openai", { type: "api_key", key: "fake-api-key", source: "login" });
		const canonical = await Bun.file(path.join(context.hermesHome, "auth.json")).json();
		expect(canonical.credential_pool["openai-api"][0].access_token).toBe("fake-api-key");
		// Another engine logs out. The stale SQLite mirror must not re-import it.
		await Bun.write(
			path.join(context.hermesHome, "auth.json"),
			JSON.stringify({ version: 1, providers: {}, credential_pool: {} }),
		);
		storage.close();
		auth = undefined;
		const reopened = await open();
		expect(reopened.store.listAuthCredentials("openai")).toEqual([]);
		expect((await Bun.file(path.join(context.hermesHome, "auth.json")).json()).credential_pool).toEqual({});
	});
	test("a selected profile never imports credentials from the default home", async () => {
		await Bun.write(
			path.join(context.hermesHome, "auth.json"),
			JSON.stringify({
				version: 1,
				providers: {
					"openai-codex": { tokens: { access_token: token("default"), refresh_token: "default-only" } },
				},
			}),
		);
		context = { ...context, hermesHome: path.join(dir, "profiles/private") };
		const { store } = await open();
		expect(store.listAuthCredentials("openai-codex")).toEqual([]);
	});

	test("a Mercury named profile inherits the main login and observes a peer's rotated grant", async () => {
		const owner = path.join(context.hermesHome, "auth.json");
		const access = token("main");
		await Bun.write(
			owner,
			JSON.stringify({
				version: 1,
				providers: {
					"openai-codex": { tokens: { access_token: access, refresh_token: "main-refresh" } },
				},
			}),
		);
		context = { ...context, hermesHome: path.join(dir, "hermes/profiles/research") };
		await Bun.write(path.join(context.hermesHome, "config.yaml"), "models: {}\n");
		const { storage, store } = await open();
		expect(await storage.getApiKey("openai-codex")).toBe(access);
		const rotated = token("main-rotated");
		await Bun.write(
			owner,
			JSON.stringify({
				version: 1,
				providers: {
					"openai-codex": { tokens: { access_token: rotated, refresh_token: "rotated-refresh" } },
				},
			}),
		);
		expect(store.pollExternalChanges?.()).toBe(true);
		expect(await storage.getApiKey("openai-codex")).toBe(rotated);
		expect(fs.existsSync(path.join(context.hermesHome, "auth.json"))).toBe(false);
	});

	test("the real factory honors an independent profile despite the inherited launcher agent directory", async () => {
		const selected = path.join(dir, "hermes/profiles/private");
		await Bun.write(path.join(selected, "config.yaml"), "profile:\n  inherit_credentials: false\n");
		const privateAccess = token("private");
		await Bun.write(
			path.join(context.hermesHome, "auth.json"),
			JSON.stringify({
				version: 1,
				providers: {
					"openai-codex": { tokens: { access_token: token("default"), refresh_token: "default-only" } },
				},
			}),
		);
		await Bun.write(
			path.join(selected, "auth.json"),
			JSON.stringify({
				version: 1,
				providers: { "openai-codex": { tokens: { access_token: privateAccess, refresh_token: "private-only" } } },
			}),
		);
		const overrides = {
			MERCURY_HOME: dir,
			HERMES_HOME: selected,
			MERCURY_REPO: context.repo,
			MERCURY_PYTHON: python!,
			PI_CODING_AGENT_DIR: path.join(dir, "omp"),
			OMP_PROFILE: undefined,
			PI_PROFILE: undefined,
		};
		const saved = new Map(Object.keys(overrides).map(key => [key, process.env[key]]));
		try {
			for (const [key, value] of Object.entries(overrides)) {
				if (value === undefined) delete process.env[key];
				else process.env[key] = value;
			}
			__resetDirsFromEnvForTests();
			expect(getAgentDbPath()).toBe(path.join(selected, "omp/agent.db"));
			auth = await AuthStorage.create(getAgentDbPath(), { usageProviderResolver: () => undefined });
			await auth.reload();
			expect(await auth.getApiKey("openai-codex")).toBe(privateAccess);
			expect(fs.existsSync(path.join(dir, "omp/agent.db"))).toBe(false);
		} finally {
			for (const [key, value] of saved) {
				if (value === undefined) delete process.env[key];
				else process.env[key] = value;
			}
			__resetDirsFromEnvForTests();
		}
	});

	test("a running OMP adopts a Hermes peer's fresh grant before returning an access token", async () => {
		const canonicalPath = path.join(context.hermesHome, "auth.json");
		await Bun.write(
			canonicalPath,
			JSON.stringify({
				version: 1,
				providers: { "openai-codex": { tokens: { access_token: token("old"), refresh_token: "old-refresh" } } },
			}),
		);
		const { storage } = await open();
		const canonical = await Bun.file(canonicalPath).json();
		const fresh = token("fresh");
		canonical.providers["openai-codex"].tokens = { access_token: fresh, refresh_token: "fresh-refresh" };
		await Bun.write(canonicalPath, JSON.stringify(canonical));
		expect(await storage.getApiKey("openai-codex")).toBe(fresh);
	});
});
