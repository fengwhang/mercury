import { expect, it } from "bun:test";
import { TempDir } from "@oh-my-pi/pi-utils";

it("does not register or ship telemetry during startup with inherited OTEL endpoints", async () => {
	using home = TempDir.createSync("omp-local-telemetry-");
	const requests: string[] = [];
	const server = Bun.serve({
		hostname: "127.0.0.1",
		port: 0,
		fetch(request) {
			requests.push(new URL(request.url).pathname);
			return new Response(null, { status: 200 });
		},
	});
	try {
		const endpoint = `http://127.0.0.1:${server.port}`;
		const child = Bun.spawn([process.execPath, `${import.meta.dir}/main-local-telemetry-probe.ts`, home.path()], {
			cwd: home.path(),
			stdout: "pipe",
			stderr: "pipe",
			env: {
				...process.env,
				HOME: home.path(),
				PI_CODING_AGENT_DIR: `${home.path()}/agent`,
				OTEL_SDK_DISABLED: "false",
				OTEL_EXPORTER_OTLP_ENDPOINT: endpoint,
				OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: `${endpoint}/v1/traces`,
				OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: `${endpoint}/v1/logs`,
				OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: `${endpoint}/v1/metrics`,
				OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf",
				OTEL_TRACES_EXPORTER: "otlp",
				OTEL_LOGS_EXPORTER: "otlp",
				OTEL_METRICS_EXPORTER: "otlp",
				OTEL_BSP_SCHEDULE_DELAY: "1",
				OTEL_BLRP_SCHEDULE_DELAY: "1",
				OTEL_METRIC_EXPORT_INTERVAL: "10",
				OTEL_LOG_LEVEL: "info",
			},
		});
		const [stdout, stderr, exitCode] = await Promise.all([
			new Response(child.stdout).text(),
			new Response(child.stderr).text(),
			child.exited,
		]);
		expect({ exitCode, stderr }).toEqual({ exitCode: 0, stderr: "" });
		expect(JSON.parse(stdout.trim())).toEqual({
			reachedSessionCreation: true,
			automaticTelemetry: false,
			recording: false,
		});
		expect(requests).toEqual([]);
	} finally {
		await server.stop(true);
	}
}, 15_000);
