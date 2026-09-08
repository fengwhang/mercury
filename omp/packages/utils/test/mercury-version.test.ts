import { describe, expect, test } from "bun:test";
import { version as forkVersion } from "../package.json" with { type: "json" };
import { USER_AGENT, VERSION } from "@oh-my-pi/pi-utils/dirs";

const dirsUrl = new URL("../src/dirs.ts", import.meta.url).href;

async function loadDirsWithEnv(
	env: Record<string, string | undefined>,
): Promise<{ VERSION: string; USER_AGENT: string }> {
	const script = [
		`const m = await import(${JSON.stringify(dirsUrl)});`,
		"console.log(m.VERSION);",
		"console.log(m.USER_AGENT);",
	].join("\n");
	const proc = Bun.spawnSync({
		cmd: ["bun", "-e", script],
		env: { ...Bun.env, ...env },
		stdout: "pipe",
		stderr: "pipe",
	});
	if (proc.exitCode !== 0) throw new Error(`dirs subprocess failed:\n${proc.stderr.toString()}`);
	const [loadedVersion, loadedAgent] = proc.stdout.toString().trim().split("\n");
	return { VERSION: loadedVersion, USER_AGENT: loadedAgent };
}

describe("Mercury version reporting (HERMES-OMP PATCH)", () => {
	test("falls back to fork version without injected MERCURY_VERSION", () => {
		expect(Bun.env.MERCURY_VERSION).toBeUndefined();
		expect(VERSION).toBe(forkVersion);
		expect(USER_AGENT).toBe(`omp/${forkVersion}`);
	});

	test("injected MERCURY_VERSION wins (compiled-binary path)", async () => {
		const loaded = await loadDirsWithEnv({ MERCURY_VERSION: "0.0.99-test" });
		expect(loaded.VERSION).toBe("0.0.99-test");
		expect(loaded.USER_AGENT).toBe("omp/0.0.99-test-mercury");
	});

	test("blank MERCURY_VERSION falls back to fork version", async () => {
		const loaded = await loadDirsWithEnv({ MERCURY_VERSION: "   " });
		expect(loaded.VERSION).toBe(forkVersion);
		expect(loaded.USER_AGENT).toBe(`omp/${forkVersion}`);
	});
});
