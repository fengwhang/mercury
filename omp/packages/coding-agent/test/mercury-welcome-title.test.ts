/**
 * HERMES-OMP PATCH (Mercury title): the welcome border carries the Mercury
 * product name while APP_NAME stays "omp" for the binary/path/server
 * surfaces that must keep resolving to dist/omp.
 */
import { describe, expect, test } from "bun:test";
import { WelcomeComponent } from "@oh-my-pi/pi-coding-agent/modes/components/welcome";
import { initTheme } from "@oh-my-pi/pi-coding-agent/modes/theme/theme";
import { APP_NAME } from "@oh-my-pi/pi-utils";

function visibleText(lines: readonly string[]): string {
	const ansi = new RegExp(String.fromCharCode(27) + "\\[[0-9;]*m", "g");
	return lines.join("\n").replace(ansi, "");
}

describe("Mercury welcome title (HERMES-OMP PATCH)", () => {
	test("border title reads `mercury v<version>`", async () => {
		await initTheme();
		const welcome = new WelcomeComponent("0.0.16", "model", "provider");
		const text = visibleText(welcome.render(100));
		expect(text).toContain(" mercury v0.0.16 ");
	});

	test("APP_NAME still names the omp binary surface", () => {
		expect(APP_NAME).toBe("omp");
	});
});
