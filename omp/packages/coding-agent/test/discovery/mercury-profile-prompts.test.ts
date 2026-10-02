import { expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { clearCache } from "../../src/capability/fs";
import { loadProjectContextFiles } from "../../src/system-prompt";

async function withProfile(run: (root: string, profile: string, cwd: string) => Promise<void>): Promise<void> {
	const temp = await fs.mkdtemp(path.join(os.tmpdir(), "mercury-profile-prompts-"));
	const root = path.join(temp, ".mercury");
	const profile = path.join(root, "hermes", "profiles", "coder");
	const cwd = path.join(temp, "project");
	const saved = new Map<string, string | undefined>();
	const env = { HOME: temp, MERCURY_HOME: root, HERMES_HOME: profile, MERCURY_PROFILE_HOME: profile };
	try {
		for (const [key, value] of Object.entries(env)) {
			saved.set(key, process.env[key]);
			process.env[key] = value;
		}
		await fs.mkdir(path.join(root, "config"), { recursive: true });
		await fs.mkdir(path.join(profile, "config"), { recursive: true });
		await fs.mkdir(cwd);
		for (const name of ["SOUL.md", "AGENTS.md", "HERMES.md", "OMP.md", "MEMORY.md", "USER.md"]) {
			await fs.writeFile(path.join(root, "config", name), `DEFAULT-PRIVATE-${name}`);
		}
		clearCache();
		await run(root, profile, cwd);
	} finally {
		for (const [key, value] of saved) {
			if (value === undefined) delete process.env[key];
			else process.env[key] = value;
		}
		clearCache();
		await fs.rm(temp, { recursive: true, force: true });
	}
}

test("normal prompt discovery composes every profile file plus project instructions", async () => {
	await withProfile(async (_root, profile, cwd) => {
		for (const name of ["SOUL.md", "AGENTS.md", "HERMES.md", "OMP.md", "MEMORY.md", "USER.md"]) {
			await fs.writeFile(path.join(profile, "config", name), `CODER-${name}`);
		}
		await fs.writeFile(path.join(cwd, "AGENTS.md"), "PROJECT-INSTRUCTIONS");
		const files = await loadProjectContextFiles({ cwd });
		const prompt = files.map(file => file.content).join("\n");
		for (const name of ["SOUL.md", "AGENTS.md", "OMP.md", "MEMORY.md", "USER.md"]) {
			expect(prompt).toContain(`CODER-${name}`);
		}
		expect(prompt).toContain("Active Mercury profile: coder");
		expect(prompt).toContain("PROJECT-INSTRUCTIONS");
		expect(prompt).not.toContain("CODER-HERMES.md");
		expect(prompt).not.toContain("DEFAULT-PRIVATE");
	});
});

test("missing and empty profile files cannot inherit the default persona", async () => {
	await withProfile(async (_root, profile, cwd) => {
		delete process.env.MERCURY_PROFILE_HOME;
		await fs.writeFile(path.join(profile, "config", "SOUL.md"), "");
		await fs.writeFile(path.join(profile, "SOUL.md"), "STALE-LEGACY-PERSONA");
		await fs.writeFile(path.join(profile, "AGENTS.md"), "LEGACY-PROFILE-RULES");
		const prompt = (await loadProjectContextFiles({ cwd })).map(file => file.content).join("\n");
		expect(prompt).toContain("LEGACY-PROFILE-RULES");
		expect(prompt).toContain("Active Mercury profile: coder");
		expect(prompt).not.toContain("DEFAULT-PRIVATE");
		expect(prompt).not.toContain("STALE-LEGACY-PERSONA");
	});
});
