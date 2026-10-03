import { expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { getAgentDir, setAgentDir } from "@oh-my-pi/pi-utils/dirs";
import { getManagedSkillsDir, writeManagedSkill } from "../../src/autolearn/managed-skills";
import { loadCapability } from "../../src/capability";
import { clearCache } from "../../src/capability/fs";
import { type Skill, skillCapability } from "../../src/capability/skill";
import "../../src/discovery/agents";
import "../../src/discovery/builtin";

for (const channel of [".mercury", ".mercury-nightly"]) {
	test(`${channel} OMP discovers the profile's bundled and learned skills without the main library`, async () => {
		const temp = await fs.mkdtemp(path.join(os.tmpdir(), "mercury-profile-skills-"));
		const root = path.join(temp, channel);
		const profile = path.join(root, "hermes", "profiles", "coder");
		const agentDir = path.join(profile, "omp", "agent");
		const cwd = path.join(temp, "project");
		const env = {
			HOME: temp,
			MERCURY_HOME: root,
			HERMES_HOME: profile,
			MERCURY_PROFILE_HOME: profile,
			MERCURY_SKILLS_DIR: path.join(root, "skills"), // stale ambient launcher value
			PI_CODING_AGENT_DIR: agentDir,
		};
		const keys = [...Object.keys(env), "OMP_PROFILE", "PI_PROFILE"];
		const saved = new Map(keys.map(key => [key, process.env[key]]));
		const originalAgentDir = getAgentDir();
		try {
			setAgentDir(agentDir);
			Object.assign(process.env, env);
			await fs.mkdir(cwd, { recursive: true });
			const library = path.join(profile, "skills", "memory", "mnemosyne-memory");
			await fs.mkdir(library, { recursive: true });
			const stockSkill = path.resolve(
				import.meta.dir,
				"../../../../../hermes/skills/memory/mnemosyne-memory/SKILL.md",
			);
			await fs.copyFile(stockSkill, path.join(library, "SKILL.md"));
			await fs.mkdir(path.join(agentDir, "skills"), { recursive: true });
			await fs.symlink(library, path.join(agentDir, "skills", "mnemosyne-memory"));
			const privateDir = path.join(root, ".agents", "skills", "main-private");
			await fs.mkdir(privateDir, { recursive: true });
			await fs.writeFile(
				path.join(privateDir, "SKILL.md"),
				"---\nname: main-private\ndescription: Main only\n---\nPrivate instructions",
			);
			const learned = await writeManagedSkill({
				action: "create",
				name: "profile-lesson",
				description: "Profile lesson",
				body: "Recall before remembering.",
			});
			expect(learned.path).toBe(path.join(profile, "skills", "omp-managed", "profile-lesson", "SKILL.md"));
			expect(getManagedSkillsDir()).toBe(path.join(profile, "skills", "omp-managed"));
			clearCache();
			const discovered = await loadCapability<Skill>(skillCapability.id, {
				cwd,
				providers: ["native", "omp-managed", "agents"],
			});
			const names = discovered.items.map(skill => skill.name);
			expect(names).toContain("mnemosyne-memory");
			expect(names).toContain("profile-lesson");
			expect(names).not.toContain("main-private");
			expect(discovered.items.find(skill => skill.name === "mnemosyne-memory")?.content).toContain(
				"mnemosyne_remember",
			);
		} finally {
			setAgentDir(originalAgentDir);
			for (const [key, value] of saved) {
				if (value === undefined) delete process.env[key];
				else process.env[key] = value;
			}
			clearCache();
			await fs.rm(temp, { recursive: true, force: true });
		}
	});
}
