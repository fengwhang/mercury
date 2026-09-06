import { describe, expect, test } from "bun:test";
import { loadBundledAgents } from "@oh-my-pi/pi-coding-agent/task/agents";

// HERMES-OMP PATCH (no model roles): read-only agent classification was
// REMOVED with the role system. The only bundled agent is "subagent" — a
// full-capability worker on the session model.
describe("bundled agent capabilities (single-role world)", () => {
	test("exactly one bundled agent: subagent", () => {
		const agents = loadBundledAgents();
		expect(agents.map(a => a.name)).toEqual(["subagent"]);
	});

	test("subagent declares unrestricted spawning and the session model", () => {
		const [agent] = loadBundledAgents();
		expect(agent.spawns).toBe("*");
		expect(String(agent.model)).toBe("*"); // "*" = inherit session model
	});
});
