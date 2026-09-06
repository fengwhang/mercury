import { describe, expect, test } from "bun:test";
import { getRoleInfo, MODEL_ROLES, MODEL_ROLE_IDS } from "@oh-my-pi/pi-coding-agent/config/model-roles";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";

// HERMES-OMP PATCH (no model roles): every historical name maps to the one
// role — "default", the session model.
describe("getRoleInfo (single-role world)", () => {
	test("exactly one role: default", () => {
		expect(MODEL_ROLE_IDS).toEqual(["default"]);
		expect(Object.keys(MODEL_ROLES)).toEqual(["default"]);
	});

	test("every name resolves to the session role", () => {
		const settings = Settings.isolated({});
		for (const name of ["default", "smol", "slow", "task", "advisor", "vision", "tiny", "plan", "commit"]) {
			expect(getRoleInfo(name, settings)).toBe(MODEL_ROLES.default);
		}
	});
});
