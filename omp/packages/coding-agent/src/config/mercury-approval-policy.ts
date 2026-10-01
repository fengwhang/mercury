/** Live OMP permission mode and inherited explicit denials in a Mercury profile. */
import * as fs from "node:fs";
import { YAML } from "bun";
import type { ApprovalMode } from "../tools/approval";

interface MercuryDenyRule {
	match: string;
	approval: "deny";
}

interface MercuryPolicy {
	mode: ApprovalMode;
	deny: MercuryDenyRule[];
}

function mapping(value: unknown, label: string): Record<string, unknown> {
	if (value === undefined || value === null) return {};
	if (typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be a mapping`);
	return value as Record<string, unknown>;
}

function nativeMode(value: unknown): ApprovalMode {
	if (value === undefined) return "yolo";
	if (value === "always-ask" || value === "write" || value === "yolo") {
		return value;
	}
	throw new Error("omp.tools.approvalMode must be always-ask, write, or yolo");
}

// Unsupported fnmatch wildcards broaden denial, matching the Python bridge.
function denyPattern(value: string): string {
	return value.replace(/\[[^\]]*\]|\?/gu, "*");
}

export class MercuryApprovalPolicy {
	#stamp?: string;
	#cached?: MercuryPolicy;
	#managedPatterns = new Set<string>();

	constructor(readonly filePath: string) {}

	read(): MercuryPolicy {
		try {
			const stat = fs.statSync(this.filePath, { bigint: true });
			const stamp = `${stat.dev}:${stat.ino}:${stat.mtimeNs}:${stat.ctimeNs}:${stat.size}`;
			if (stamp === this.#stamp && this.#cached) return this.#cached;
			const text = fs.readFileSync(this.filePath, "utf8");
			const config = mapping(YAML.parse(text), "Mercury config");
			const omp = mapping(config.omp, "omp");
			const tools = mapping(omp.tools, "omp.tools");
			const hermes = mapping(config.hermes, "hermes");
			const legacy = mapping(hermes.approvals, "hermes.approvals");
			const shared = mapping(config.approvals, "approvals");
			const patterns = new Set<string>();
			for (const policy of [legacy, shared]) {
				const deny = policy.deny ?? [];
				if (!Array.isArray(deny) || deny.some(rule => typeof rule !== "string")) {
					throw new Error("approvals.deny must be a list of command patterns");
				}
				for (const rule of deny as string[]) if (rule) patterns.add(denyPattern(rule));
			}
			const owned = text.match(/^\s*# Mercury inherited deny patterns: (.+)$/mu);
			if (owned) {
				const values: unknown = JSON.parse(owned[1]);
				if (Array.isArray(values)) {
					for (const value of values) {
						if (value && typeof value === "object" && !Array.isArray(value)) {
							const rule = value as Record<string, unknown>;
							if (rule.approval === "deny" && typeof rule.match === "string")
								this.#managedPatterns.add(rule.match);
						}
					}
				}
			}
			this.#cached = {
				mode: nativeMode(tools.approvalMode),
				deny: [...patterns].map(match => ({ match, approval: "deny" })),
			};
			this.#stamp = stamp;
			return this.#cached;
		} catch (error) {
			// Never keep a previously permissive snapshot after an invalid write.
			throw new Error(`Cannot enforce Mercury approval policy: ${String(error)}`);
		}
	}

	mergePatterns(value: unknown): unknown[] {
		const policy = this.read();
		const existing = Array.isArray(value) ? value : [];
		return [
			...existing.filter(rule => {
				if (!rule || typeof rule !== "object") return true;
				const record = rule as Record<string, unknown>;
				return !(
					record.approval === "deny" &&
					typeof record.match === "string" &&
					this.#managedPatterns.has(record.match)
				);
			}),
			...policy.deny,
		];
	}
}
