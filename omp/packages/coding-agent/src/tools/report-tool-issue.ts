/**
 * report_issue — automated QA backend for tracking unexpected tool behavior.
 *
 * No model-facing tool schema anymore: the write tool dispatches plain text to
 * `xd://report_issue`, and the system prompt tells the model to write
 * `<tool>: <concise description>` there when auto-QA is enabled.
 *
 * Enabled by default (`dev.autoqa` defaults to true); `PI_AUTO_QA=0` or an
 * explicit `dev.autoqa: false` disables injection and recording.
 * Reports are stored only in the local SQLite database for inspection with
 * `mercury omp grievances`. No consent dialog or remote submission is involved.
 */
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";
import type { AgentToolResult } from "@oh-my-pi/pi-agent-core";
import type { Component } from "@oh-my-pi/pi-tui";
import { Text } from "@oh-my-pi/pi-tui";
import { $flag, getAutoQaDbPath, logger, VERSION } from "@oh-my-pi/pi-utils";
import type { Settings } from "..";
import type { Theme } from "../modes/theme/theme";
import { renderStatusLine, truncateToWidth } from "../tui";
import type { ToolSession } from "./index";
import { replaceTabs } from "./render-utils";
import { ToolError } from "./tool-errors";
import type { XdevDispatch } from "./xdev";

export const REPORT_ISSUE_DEVICE_NAME = "report_issue";
export const REPORT_ISSUE_DEVICE_PATH = `xd://${REPORT_ISSUE_DEVICE_NAME}`;

/** Usage text for `read xd://report_issue`. */
export function reportIssueDeviceUsage(): string {
	return `Write \`<tool>: <concise description>\` as plain text to ${REPORT_ISSUE_DEVICE_PATH}. A two-line fallback also works: tool name on line 1, report body below.`;
}

/** Whether a tool call writes to `xd://report_issue`. */
export function isReportIssueToolCall(toolCall: { name: string; arguments?: Record<string, unknown> }): boolean {
	if (toolCall.name !== "write") return false;
	const args = toolCall.arguments;
	const path =
		typeof args?.path === "string" ? args.path : typeof args?.file_path === "string" ? args.file_path : undefined;
	return path === REPORT_ISSUE_DEVICE_PATH || path === `${REPORT_ISSUE_DEVICE_PATH}/`;
}

/** Call preview for an `xd://report_issue` write. */
export function renderReportIssueDeviceCall(content: unknown, uiTheme: Theme): Component {
	const body = typeof content === "string" ? replaceTabs(content.trim().split("\n")[0] ?? "") : "";
	const text = renderStatusLine(
		{
			icon: "pending",
			title: "Report Tool Issue",
			description: body ? truncateToWidth(body, 72) : undefined,
		},
		uiTheme,
	);
	return new Text(text, 0, 0);
}

function parseReportIssueBody(text: string): { tool: string; report: string } {
	const body = text.trim();
	if (!body) {
		throw new ToolError(`Empty report. ${reportIssueDeviceUsage()}`);
	}
	const firstNewline = body.indexOf("\n");
	if (firstNewline >= 0) {
		const tool = body.slice(0, firstNewline).trim();
		const report = body.slice(firstNewline + 1).trim();
		if (tool && report) return { tool, report };
	}
	const colon = body.indexOf(":");
	if (colon > 0) {
		const tool = body.slice(0, colon).trim();
		const report = body.slice(colon + 1).trim();
		if (tool && report) return { tool, report };
	}
	throw new ToolError(`Invalid report format. ${reportIssueDeviceUsage()}`);
}

/**
 * Whether Auto-QA is active for this session.
 *
 * Precedence: `PI_AUTO_QA` env flag > `dev.autoqa` setting (on by default).
 */
export function isAutoQaEnabled(settings?: Settings): boolean {
	return $flag("PI_AUTO_QA", settings?.get("dev.autoqa") === true);
}

/**
 * Open the local auto-QA SQLite database at
 * `~/.omp/autoqa.db` (XDG: `$XDG_DATA_HOME/omp/autoqa.db`), creating the
 * schema lazily. The caller owns the handle. Returns `null` on failure.
 */
export function openAutoQaDb(): Database | null {
	const dbPath = getAutoQaDbPath();
	if (!dbPath) return null;
	try {
		fs.mkdirSync(path.dirname(dbPath), { recursive: true });
		const db = new Database(dbPath, { create: true });
		// Install the busy handler BEFORE any lock-taking statement. See #2421.
		db.run("PRAGMA busy_timeout = 5000");
		db.exec(`
			CREATE TABLE IF NOT EXISTS grievances (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				model TEXT NOT NULL,
				version TEXT NOT NULL,
				tool TEXT NOT NULL,
				report TEXT NOT NULL,
				created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
			);
		`);
		// Legacy DBs (May 2026) predate `created_at`. ALTER TABLE only accepts
		// constant defaults, so add it empty and backfill.
		const hasCreatedAt = db.prepare("SELECT 1 FROM pragma_table_info('grievances') WHERE name = 'created_at'").get();
		if (!hasCreatedAt) {
			db.exec(`
				ALTER TABLE grievances ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
				UPDATE grievances SET created_at = CURRENT_TIMESTAMP WHERE created_at = '';
			`);
		}
		return db;
	} catch (error) {
		logger.warn("Failed to open auto-QA database", { error: String(error) });
		return null;
	}
}

function recordToolIssue(session: ToolSession, tool: string, report: string): boolean {
	const canonicalTool = tool.startsWith("proxy_") ? tool.slice("proxy_".length) : tool;
	const model = session.getActiveModelString?.() ?? "unknown";
	const db = openAutoQaDb();
	if (!db) return false;
	try {
		db.prepare(
			"INSERT INTO grievances (model, version, tool, report, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
		).run(model, VERSION, canonicalTool, report);
		return true;
	} catch (error) {
		logger.error("Failed to record tool issue locally", { error: String(error) });
		return false;
	} finally {
		db.close();
	}
}

/**
 * Execute `write xd://report_issue`. `text` must be either:
 * - `<tool>: <concise description>` on one line, or
 * - tool name on the first line with the report body below.
 */
export async function dispatchReportIssueDevice(
	session: ToolSession,
	text: string,
): Promise<{ result: AgentToolResult<unknown>; xdev: XdevDispatch }> {
	let message = "Auto QA is disabled; issue not recorded.";
	if (isAutoQaEnabled(session.settings)) {
		const { tool, report } = parseReportIssueBody(text);
		message = recordToolIssue(session, tool, report) ? "Recorded locally." : "Could not record issue locally.";
	}
	return {
		result: { content: [{ type: "text", text: message }] },
		xdev: { tool: REPORT_ISSUE_DEVICE_NAME, mode: "execute", args: { report: text.trim() } },
	};
}
