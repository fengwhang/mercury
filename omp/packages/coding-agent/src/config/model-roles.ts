/**
 * Model roles — KILLED (HERMES-OMP PATCH, user decision 2026-09-04).
 *
 * Mercury has NO model role system. There is no role routing, no
 * per-role model selection, no role auto-selection. The only two model
 * settings at the omp layer are:
 *
 *   delegateModel    — the model delegated from hermes to omp
 *   delegateFallback — its required fallback
 *
 * Every historical role name resolves to the session model. The exports
 * below are kept as compile-compatible stubs so the ~30 peripheral files
 * that reference role *names* (UI listings, help text) keep building;
 * they are inert. Sweep them at leisure (tracked in TODO A1-sweep).
 */
import type { ThemeColor } from "../modes/theme/schema";
import type { Settings } from "./settings";

export const MODEL_ROLE_ALIAS_PREFIX = "@";
export const LEGACY_MODEL_ROLE_ALIAS_PREFIX = "pi/";
export const DEFAULT_MODEL_ROLE_ALIAS = "*";

export function formatModelRoleAlias(role: string): string {
	return `${MODEL_ROLE_ALIAS_PREFIX}${role}`;
}

/**
 * HERMES-OMP PATCH (no model roles): "default" is the ONLY role — the
 * session model (Mercury: delegate model + fallback chain). Historical
 * names are accepted as INPUT aliases (see isSessionInheritedAgentPattern)
 * but the type no longer admits them.
 */
export type ModelRole = "default";

export interface ModelRoleInfo {
	id: string;
	tag: string;
	name: string;
	color: ThemeColor;
	/** Present for source-plumbing reasons; always false in the single-role world. */
	hidden?: boolean;
}

/** The single role that exists: default. Lookups of any id return this. */
export const MODEL_ROLES: Record<string, ModelRoleInfo> = {
	default: { id: "default", tag: "DEF", name: "Session", color: "accent" },
};

export const MODEL_ROLE_IDS: string[] = ["default"];

export type RoleInfo = ModelRoleInfo;

/** Only "default" is known; every other id is unknown (no role machinery). */
export function getKnownRoleIds(_settings: Settings): string[] {
	return ["default"];
}

export function getRoleInfo(role: string, _settings: Settings): RoleInfo {
	// Every role name maps to the one role: the session model.
	return MODEL_ROLES.default;
}

/**
 * Historical per-role user assignment map — permanently empty. modelRoles
 * was REMOVED from settings-schema; this returns nothing for every input
 * so any stale reader sees "no assignments" rather than data.
 */
export function getUserRoleAssignments(_settings: Settings): Record<string, string> {
	return {};
}
