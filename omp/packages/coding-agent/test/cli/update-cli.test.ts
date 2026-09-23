import { afterEach, describe, expect, it, vi } from "bun:test";
import { getLatestRelease, runUpdateCommand } from "../../src/cli/update-cli";

type FetchInput = string | URL | Request;
type FetchInit = RequestInit | BunFetchRequestInit;

describe("runUpdateCommand fetch cancellation", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("checks release metadata with a timeout signal", async () => {
		let requestSignal: AbortSignal | undefined;
		vi.spyOn(console, "log").mockImplementation(() => {});
		const fetchStub = Object.assign(
			async (_input: FetchInput, init?: FetchInit) => {
				requestSignal = init?.signal ?? undefined;
				return Response.json({ version: "999.0.0" });
			},
			{ preconnect: globalThis.fetch.preconnect },
		);
		vi.spyOn(globalThis, "fetch").mockImplementation(fetchStub);

		await runUpdateCommand({ force: false, check: true });

		expect(requestSignal).toBeInstanceOf(AbortSignal);
	});
});

describe("getLatestRelease rename pointers", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	function stubRegistry(manifests: Record<string, unknown>): string[] {
		const urls: string[] = [];
		const fetchStub = Object.assign(
			async (input: FetchInput) => {
				const url = String(input);
				urls.push(url);
				let manifest: unknown;
				for (const pkg in manifests) {
					if (url.includes(pkg)) {
						manifest = manifests[pkg];
						break;
					}
				}
				if (!manifest) return new Response(null, { status: 404, statusText: "Not Found" });
				return Response.json(manifest);
			},
			{ preconnect: globalThis.fetch.preconnect },
		);
		vi.spyOn(globalThis, "fetch").mockImplementation(fetchStub);
		return urls;
	}

	it("follows omp.rename to the new package and resolves version, dist, and names from its manifest", async () => {
		const urls = stubRegistry({
			"@new/omp": { version: "999.1.0", omp: { dist: "npm" } },
			"@oh-my-pi/pi-coding-agent": {
				version: "999.0.0",
				omp: { dist: "binary", rename: { package: "@new/omp", natives: "@new/natives" } },
			},
		});

		const release = await getLatestRelease();

		expect(release.version).toBe("999.1.0");
		expect(release.dist).toBe("npm");
		expect(release.packages).toEqual({ pkg: "@new/omp", natives: "@new/natives" });
		expect(urls).toEqual([
			"https://registry.npmjs.org/@oh-my-pi/pi-coding-agent/latest",
			"https://registry.npmjs.org/@new/omp/latest",
		]);
	});
	it("fetches the canary dist-tag when checking the canary channel", async () => {
		const urls = stubRegistry({
			"@oh-my-pi/pi-coding-agent": { version: "999.0.0-canary.1" },
		});

		await getLatestRelease({ channel: "canary" });

		expect(urls).toEqual(["https://registry.npmjs.org/@oh-my-pi/pi-coding-agent/canary"]);
	});

	it("ignores a rename pointer that cycles back to an already-visited package", async () => {
		const urls = stubRegistry({
			"@oh-my-pi/pi-coding-agent": {
				version: "999.0.0",
				omp: { rename: { package: "@oh-my-pi/pi-coding-agent" } },
			},
		});

		const release = await getLatestRelease();

		expect(urls).toHaveLength(1);
		expect(release.version).toBe("999.0.0");
		expect(release.packages).toEqual({ pkg: "@oh-my-pi/pi-coding-agent", natives: "@oh-my-pi/pi-natives" });
	});
});

describe("getLatestRelease proxy errors", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("translates Bun's UnsupportedProxyProtocol fetch failure into an actionable CLI message", async () => {
		const fetchStub = Object.assign(
			async () => {
				throw new Error(
					'UnsupportedProxyProtocol fetching "https://registry.npmjs.org/@oh-my-pi/pi-coding-agent/latest". ' +
						"For more information, pass `verbose: true` in the second argument to fetch()",
				);
			},
			{ preconnect: globalThis.fetch.preconnect },
		);
		vi.spyOn(globalThis, "fetch").mockImplementation(fetchStub);

		const err = await getLatestRelease({ timeoutMs: 5000 }).then(
			() => null,
			(e: unknown) => e as Error,
		);

		expect(err).toBeInstanceOf(Error);
		// The raw fetch() instruction the CLI user cannot act on must not leak through.
		expect(err?.message).not.toContain("verbose: true");
		expect(err?.message).not.toContain("fetch()");
		// Instead the user gets actionable guidance about supported proxy schemes.
		expect(err?.message).toMatch(/SOCKS/i);
		expect(err?.message).toMatch(/https?:\/\//i);
	});
});

describe("Mercury builds skip the omp update flow", () => {
	const savedMercuryVersion = process.env.MERCURY_VERSION;
	afterEach(() => {
		vi.restoreAllMocks();
		if (savedMercuryVersion === undefined) delete process.env.MERCURY_VERSION;
		else process.env.MERCURY_VERSION = savedMercuryVersion;
	});

	function stubUnreachableFetch() {
		const fetchStub = Object.assign(
			async () => {
				throw new Error("network must not be touched on Mercury builds");
			},
			{ preconnect: globalThis.fetch.preconnect },
		);
		return vi.spyOn(globalThis, "fetch").mockImplementation(fetchStub);
	}

	it("runUpdateCommand prints the mercury-update pointer and never hits the network", async () => {
		process.env.MERCURY_VERSION = "9.9.9-mercury-test";
		const logs: string[] = [];
		vi.spyOn(console, "log").mockImplementation((...args: unknown[]) => {
			logs.push(args.map(String).join(" "));
		});
		const fetchSpy = stubUnreachableFetch();
		const exitSpy = vi.spyOn(process, "exit").mockImplementation((() => {
			throw new Error("process.exit must not be called on Mercury builds");
		}) as () => never);

		// Both the check path and the install path must no-op.
		await runUpdateCommand({ force: false, check: true });
		await runUpdateCommand({ force: true, check: false });

		expect(fetchSpy).not.toHaveBeenCalled();
		expect(exitSpy).not.toHaveBeenCalled();
		expect(logs.join("\n")).toContain("mercury update");
		expect(logs.join("\n")).toContain("Fengwhang/mercury");
	});

	it("getLatestRelease rejects with the pointer without fetching", async () => {
		process.env.MERCURY_VERSION = "9.9.9-mercury-test";
		const fetchSpy = stubUnreachableFetch();

		const err = await getLatestRelease().then(
			() => null,
			(e: unknown) => e as Error,
		);

		expect(fetchSpy).not.toHaveBeenCalled();
		expect(err?.message).toContain("mercury update");
	});

	it("dev path still queries the registry when MERCURY_VERSION is unset", async () => {
		delete process.env.MERCURY_VERSION;
		const urls: string[] = [];
		const fetchStub = Object.assign(
			async (input: FetchInput) => {
				urls.push(String(input));
				return Response.json({ version: "999.0.0" });
			},
			{ preconnect: globalThis.fetch.preconnect },
		);
		vi.spyOn(globalThis, "fetch").mockImplementation(fetchStub);
		vi.spyOn(console, "log").mockImplementation(() => {});

		const release = await getLatestRelease();
		await runUpdateCommand({ force: false, check: true });

		expect(release.version).toBe("999.0.0");
		// One fetch for the direct call above, one for the --check run below.
		expect(urls).toEqual([
			"https://registry.npmjs.org/@oh-my-pi/pi-coding-agent/latest",
			"https://registry.npmjs.org/@oh-my-pi/pi-coding-agent/latest",
		]);
	});
});
