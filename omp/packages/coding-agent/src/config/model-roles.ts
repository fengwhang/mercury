/**
 * Mercury exposes one model identity: task (the session model and its fallback chain).
 * Per-role routing, custom assignments, and automatic role selection remain removed.
 * The remaining exports keep older callers compatible without adding other roles.
 */
import type { ThemeColor } from "../modes/theme/schema";
import type { Settings } from "./settings";

export const MODEL_ROLE_ALIAS_PREFIX = "@";
export const LEGACY_MODEL_ROLE_ALIAS_PREFIX = "pi/";
export const DEFAULT_MODEL_ROLE_ALIAS = "*";

export function formatModelRoleAlias(role: string): string {
	return `${MODEL_ROLE_ALIAS_PREFIX}${role}`;
}

/** Mercury uses one task model and its fallback chain; role routing stays removed. */
export type ModelRole = "task";

export interface ModelRoleInfo {
	id: string;
	tag: string;
	name: string;
	color: ThemeColor;
	/** Present for source-plumbing reasons; always false in the single-role world. */
	hidden?: boolean;
}

/** The single task model label. Legacy ids display this same label. */
export const MODEL_ROLES: Record<string, ModelRoleInfo> = {
	task: { id: "task", tag: "TASK", name: "Task", color: "accent" },
};

export const MODEL_ROLE_IDS: string[] = ["task"];

export type RoleInfo = ModelRoleInfo;

/** Only "task" is known; role routing remains disabled. */
export function getKnownRoleIds(_settings: Settings): string[] {
	return ["task"];
}

export function getRoleInfo(_role: string, _settings: Settings): RoleInfo {
	// Every role name maps to the one role: the session model.
	return MODEL_ROLES.task;
}

/**
 * Historical per-role user assignment map — permanently empty. modelRoles
 * was REMOVED from settings-schema; this returns nothing for every input
 * so any stale reader sees "no assignments" rather than data.
 */
export function getUserRoleAssignments(_settings: Settings): Record<string, string> {
	return {};
}
