/** Mercury's profile-local credential authority; private stdin/stdout IPC, never argv. */
import * as fs from "node:fs";
import * as path from "node:path";
import { getAgentDbPath } from "@oh-my-pi/pi-utils";
import type { AuthCredential, AuthCredentialStore, OAuthCredential, StoredAuthCredential } from "../auth-storage";
import { getOAuthProvider, refreshOAuthToken } from "../registry/oauth";
import type { OAuthCredentials, OAuthProvider } from "../registry/oauth/types";
import type { Api, ModelSpec } from "../types";

interface SharedRecord {
	id: string;
	provider: string;
	credential: AuthCredential;
}
interface SharedSnapshot {
	records: SharedRecord[];
	oauthProviders: string[];
	apiKeyProviders: string[];
}
export interface MercuryCredentialContext {
	home: string;
	hermesHome: string;
	repo: string;
	python: string;
}

export function mercuryCredentialContext(dbPath: string): MercuryCredentialContext | undefined {
	const home = process.env.MERCURY_HOME?.trim();
	const repo = process.env.MERCURY_REPO?.trim();
	if (!home || !repo || dbPath === ":memory:") return undefined;
	const hermesHome = process.env.HERMES_HOME?.trim() || path.join(home, "hermes");
	const root = path.resolve(home);
	const active = path.resolve(hermesHome);
	// Explicit SDK stores outside this install must never adopt the operator's credentials.
	if (path.resolve(dbPath) !== path.resolve(getAgentDbPath())) return undefined;
	// Named OMP profiles without a matching Hermes home are intentionally independent.
	if ((process.env.OMP_PROFILE || process.env.PI_PROFILE) && active === path.join(root, "hermes")) return undefined;
	return { home, hermesHome, repo, python: process.env.MERCURY_PYTHON?.trim() || "python3" };
}

async function exchangeRaw(
	context: MercuryCredentialContext,
	request: Record<string, unknown>,
	signal?: AbortSignal,
): Promise<unknown> {
	if (signal?.aborted) throw new Error("Credential interchange aborted");
	const child = Bun.spawn([context.python, "-m", "mercury_cli.provider_sync"], {
		cwd: context.repo,
		env: {
			...process.env,
			MERCURY_HOME: context.home,
			HERMES_HOME: context.hermesHome,
			MERCURY_AUTH_IPC: "1",
			PYTHONPATH: [path.join(context.repo, "hermes"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
		},
		stdin: "pipe",
		stdout: "pipe",
		stderr: "pipe",
	});
	const abort = () => child.kill();
	signal?.addEventListener("abort", abort, { once: true });
	const timer = setTimeout(abort, 60_000);
	try {
		child.stdin.write(JSON.stringify(request));
		child.stdin.end();
		const [output, , exit] = await Promise.all([
			new Response(child.stdout).text(),
			new Response(child.stderr).text(),
			child.exited,
		]);
		// Neither response bodies nor credential-bearing requests go into exception/log text.
		if (exit !== 0) throw new Error("Mercury credential interchange failed; check provider login status");
		try {
			return JSON.parse(output);
		} catch {
			throw new Error("Invalid Mercury credential interchange response");
		}
	} finally {
		clearTimeout(timer);
		signal?.removeEventListener("abort", abort);
	}
}

export interface MercuryRuntimeProvider {
	provider: string;
	baseUrl: string;
	apiKey: string;
	models: (Omit<ModelSpec<Api>, "provider" | "contextWindow" | "maxTokens"> & {
		contextWindow: number;
		maxTokens: number;
	})[];
}

export async function resolveMercuryRuntimeProvider(
	context: MercuryCredentialContext,
	provider: string,
	includeModels = true,
	forceRefresh = false,
	signal?: AbortSignal,
): Promise<MercuryRuntimeProvider | undefined> {
	const response = await exchangeRaw(
		context,
		{ operation: "runtime-provider", provider, includeModels, forceRefresh },
		signal,
	);
	const runtime = (response as { runtime?: MercuryRuntimeProvider | null })?.runtime;
	if (!runtime) return undefined;
	if (
		runtime.provider !== provider ||
		typeof runtime.apiKey !== "string" ||
		typeof runtime.baseUrl !== "string" ||
		!Array.isArray(runtime.models)
	) {
		throw new Error("Invalid Mercury runtime provider response");
	}
	return runtime;
}

async function exchange(
	context: MercuryCredentialContext,
	request: Record<string, unknown>,
	signal?: AbortSignal,
): Promise<SharedSnapshot> {
	const snapshot = (await exchangeRaw(context, request, signal)) as SharedSnapshot;
	if (
		!snapshot ||
		!Array.isArray(snapshot.records) ||
		!Array.isArray(snapshot.oauthProviders) ||
		!Array.isArray(snapshot.apiKeyProviders)
	) {
		throw new Error("Invalid Mercury credential interchange response");
	}
	return snapshot;
}

/** Keep usage/backoff in SQLite, but shared grants and refresh authority in Hermes. */
export function mercuryCredentialStore(
	store: AuthCredentialStore,
	context: MercuryCredentialContext,
): AuthCredentialStore {
	let snapshot: SharedSnapshot = { records: [], oauthProviders: [], apiKeyProviders: [] };
	let migrated = false;
	let lastMtime = "";
	let inFlight: Promise<void> | undefined;
	const bindings = new Map<number, string>();
	const managed = new Set<number>();
	for (const row of store.listAuthCredentials()) {
		if (row.credential.mercuryAuthId) {
			managed.add(row.id);
			bindings.set(row.id, row.credential.mercuryAuthId);
		}
	}
	const mtime = () => {
		return ["auth.json", ".anthropic_oauth.json"]
			.map(file => {
				try {
					const stat = fs.statSync(path.join(context.hermesHome, file));
					return `${stat.mtimeMs}:${stat.size}`;
				} catch (error) {
					if ((error as NodeJS.ErrnoException).code === "ENOENT") return "missing";
					throw error;
				}
			})
			.join(":");
	};
	const shared = (provider: string, credential?: AuthCredential) =>
		credential === undefined
			? snapshot.apiKeyProviders.includes(provider) || snapshot.oauthProviders.includes(provider)
			: credential.type === "oauth"
				? snapshot.oauthProviders.includes(provider)
				: snapshot.apiKeyProviders.includes(provider);
	const apply = (next: SharedSnapshot) => {
		snapshot = next;
		const previous = new Set(managed);
		bindings.clear();
		managed.clear();
		const groups = new Map<string, SharedRecord[]>();
		for (const record of next.records) {
			const group = groups.get(record.provider) ?? [];
			group.push(record);
			groups.set(record.provider, group);
		}
		for (const [provider, records] of groups) {
			const native = store
				.listAuthCredentials(provider)
				.filter(row => !row.credential.mercuryAuthId && !shared(provider, row.credential));
			const rows = store.replaceAuthCredentialsForProvider(provider, [
				...records.map(record => ({ ...record.credential, mercuryAuthId: record.id })),
				...native.map(row => row.credential),
			]);
			for (const row of rows) {
				if (row.credential.mercuryAuthId) {
					bindings.set(row.id, row.credential.mercuryAuthId);
					managed.add(row.id);
				}
			}
		}
		for (const id of previous)
			if (!managed.has(id)) store.deleteAuthCredential(id, "removed from Mercury shared auth");
		lastMtime = mtime();
	};
	const synchronize = async () => {
		if (inFlight) return inFlight;
		inFlight = (async () => {
			let next = await exchange(context, { operation: "snapshot" });
			snapshot = next;
			if (!migrated) {
				// Adopt old native logins once, without overwriting fresher canonical grants.
				for (const row of store.listAuthCredentials()) {
					if (!row.credential.mercuryAuthId && shared(row.provider, row.credential))
						next = await exchange(context, {
							operation: "adopt",
							provider: row.provider,
							credentials: [row.credential],
						});
				}
				migrated = true;
			}
			apply(next);
		})().finally(() => {
			inFlight = undefined;
		});
		return inFlight;
	};
	const mutate = async (
		operation: string,
		provider: string,
		credentials: AuthCredential[],
	): Promise<StoredAuthCredential[]> => {
		if (!shared(provider, credentials[0])) {
			return operation === "replace"
				? store.replaceAuthCredentialsForProvider(provider, credentials)
				: store.upsertAuthCredentialForProvider(provider, credentials[0]!);
		}
		apply(await exchange(context, { operation, provider, credentials }));
		return store.listAuthCredentials(provider);
	};
	const hooks: Partial<AuthCredentialStore> = {
		synchronizeCredentials: synchronize,
		pollExternalChanges: () => mtime() !== lastMtime || (store.pollExternalChanges?.() ?? false),
		prepareForRequest: async (_id, options) => {
			if (options?.signal?.aborted) throw new Error("Credential interchange aborted");
			await synchronize();
			return true;
		},
		upsertAuthCredentialRemote: (provider, credential) => mutate("upsert", provider, [credential]),
		replaceAuthCredentialsRemote: (provider, credentials) => mutate("replace", provider, credentials),
		deleteAuthCredentialsRemote: async (provider, cause) => {
			if (shared(provider)) apply(await exchange(context, { operation: "remove", provider }));
			store.deleteAuthCredentialsForProvider(provider, cause);
		},
		deleteAuthCredentialRemote: async (id, cause) => {
			const row = store.listAuthCredentials().find(row => row.id === id);
			const binding = bindings.get(id);
			if (row && binding)
				apply(await exchange(context, { operation: "remove", provider: row.provider, id: binding }));
			store.deleteAuthCredential(id, cause);
			return row !== undefined;
		},
		refreshOAuthCredential: async (provider, id, credential: OAuthCredential, signal): Promise<OAuthCredentials> => {
			const binding = bindings.get(id);
			if (!binding) {
				// Non-interoperable OAuth flows remain native, never mislabelled as API keys.
				const native = getOAuthProvider(provider);
				return native?.refreshToken
					? native.refreshToken(credential, signal)
					: refreshOAuthToken(provider as OAuthProvider, credential, signal);
			}
			const next = await exchange(
				context,
				{ operation: "refresh", provider, id: binding, observedRefresh: credential.refresh },
				signal,
			);
			const record = next.records.find(record => record.id === binding);
			if (record?.credential.type !== "oauth") throw new Error("Shared OAuth credential was removed");
			// The canonical refresh is already committed. AuthStorage's own SQLite
			// lease/CAS now persists this pair; do not steal that CAS in apply().
			return record.credential;
		},
	};
	return new Proxy(store, {
		get(target, property) {
			const hook: unknown = Reflect.get(hooks, property);
			if (hook !== undefined) return hook;
			const value: unknown = Reflect.get(target, property);
			return typeof value === "function" ? value.bind(target) : value;
		},
	});
}
