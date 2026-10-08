import { expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { ModelRegistry } from "../src/config/model-registry";
import { AuthStorage } from "../src/session/auth-storage";

import { scheduleMarketplaceAutoUpdate } from "../src/extensibility/plugins/marketplace-auto-update";
import { ShareFileTool } from "../src/tools/share-file";
test("background startup requests local-only inventory even with online override", async () => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "nightly-offline-"));
	const auth = await AuthStorage.create(":memory:");
	try {
		const registry = new ModelRegistry(auth, path.join(dir, "models.yml"));
		const strategies: string[] = [];
		registry.refresh = async strategy => {
			strategies.push(strategy ?? "online-if-uncached");
		};
		registry.refreshInBackground("online");
		await registry.awaitBackgroundRefresh();
		expect(strategies).toEqual(["offline"]);
	} finally {
		auth.close();
		fs.rmSync(dir, { recursive: true, force: true });
	}
});

// Synchronize the scheduler's lazy module-loading boundary without wall-clock sleeps.
await import("../src/extensibility/plugins/marketplace");
test("marketplace startup never resolves update targets", async () => {
	let resolutions = 0;
	scheduleMarketplaceAutoUpdate({
		autoUpdate: "auto",
		resolveActiveProjectRegistryPath: async () => {
			resolutions++;
			return null;
		},
		clearPluginRootsCache: () => {},
	});
	await import("../src/extensibility/plugins/marketplace");
	await Promise.resolve();
	expect(resolutions).toBe(0);
});

test("unreviewed OMP sharing is refused without staging a file", async () => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "nightly-share-"));
	const oldHome = Bun.env.MERCURY_HOME;
	Bun.env.MERCURY_HOME = path.join(dir, "mercury-home");
	const file = path.join(dir, "artifact.txt");
	fs.writeFileSync(file, "fixture");
	try {
		const result = await new ShareFileTool().execute("fixture", { path: file });
		expect(result.details?.url).toBe("");
		expect(fs.existsSync(path.join(Bun.env.MERCURY_HOME, "observatory"))).toBe(false);
	} finally {
		if (oldHome === undefined) delete Bun.env.MERCURY_HOME;
		else Bun.env.MERCURY_HOME = oldHome;
		fs.rmSync(dir, { recursive: true, force: true });
	}
});
