import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";
import {
	dispatchReportIssueDevice,
	isAutoQaEnabled,
	isReportIssueToolCall,
	reportIssueDeviceUsage,
} from "@oh-my-pi/pi-coding-agent/tools/report-tool-issue";

let originalAutoQa: string | undefined;
beforeEach(() => {
	originalAutoQa = process.env.PI_AUTO_QA;
	delete process.env.PI_AUTO_QA;
});
afterEach(() => {
	if (originalAutoQa === undefined) delete process.env.PI_AUTO_QA;
	else process.env.PI_AUTO_QA = originalAutoQa;
});

describe("local Auto-QA enablement", () => {
	it("defaults to on when settings exist", () => {
		expect(isAutoQaEnabled(Settings.isolated())).toBe(true);
	});
	it("is off without settings", () => {
		expect(isAutoQaEnabled()).toBe(false);
	});
	it("honors an explicit disabled setting", () => {
		expect(isAutoQaEnabled(Settings.isolated({ "dev.autoqa": false }))).toBe(false);
	});
	it("allows the environment to disable recording", () => {
		process.env.PI_AUTO_QA = "0";
		expect(isAutoQaEnabled(Settings.isolated({ "dev.autoqa": true }))).toBe(false);
	});
	it("allows the environment to enable recording", () => {
		process.env.PI_AUTO_QA = "1";
		expect(isAutoQaEnabled(Settings.isolated({ "dev.autoqa": false }))).toBe(true);
	});
});

describe("report issue device validation", () => {
	it("recognizes write device calls and the file_path alias", () => {
		expect(isReportIssueToolCall({ name: "write", arguments: { path: "xd://report_issue" } })).toBe(true);
		expect(isReportIssueToolCall({ name: "write", arguments: { file_path: "xd://report_issue/" } })).toBe(true);
		expect(isReportIssueToolCall({ name: "read", arguments: { path: "xd://report_issue" } })).toBe(false);
		expect(isReportIssueToolCall({ name: "write", arguments: { path: "issue.txt" } })).toBe(false);
	});
	for (const body of ["", "just a vague sentence", "read:", "\nread\n"]) {
		it(`rejects malformed report ${JSON.stringify(body)} with usage`, async () => {
			const session = { settings: Settings.isolated() } as ToolSession;
			await expect(dispatchReportIssueDevice(session, body)).rejects.toThrow(reportIssueDeviceUsage());
		});
	}
	it("honestly reports disabled recording", async () => {
		const session = { settings: Settings.isolated({ "dev.autoqa": false }) } as ToolSession;
		const response = await dispatchReportIssueDevice(session, "read: lost a line");
		expect(response.result.content).toEqual([{ type: "text", text: "Auto QA is disabled; issue not recorded." }]);
	});
});
