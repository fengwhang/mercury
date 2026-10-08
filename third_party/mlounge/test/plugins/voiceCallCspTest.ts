import {expect, it} from "vitest";
import {once} from "events";
import Config from "../../server/config";
import start from "../../server/server";

it("permits canonical call data audio without changing other security directives", async () => {
	const port = Config.values.port;
	const prefetch = Config.values.prefetch;
	Config.values.prefetch = false;
	Config.values.port = 0;
	const server = await start({dev: false});

	try {
		if (!server.listening) {
			await once(server, "listening");
		}

		const address = server.address();

		if (!address || typeof address === "string") {
			throw new Error("expected TCP address");
		}

		const response = await fetch(`http://127.0.0.1:${address.port}/`);
		expect(response.status).toBe(200);
		const directives = response.headers.get("content-security-policy")!.split("; ");
		expect(directives.find((value) => value.startsWith("media-src "))).toBe(
			"media-src 'self' https: data:"
		);
		expect(directives.filter((value) => !value.startsWith("media-src "))).toEqual([
			"block-all-mixed-content",
			"default-src 'none'",
			"base-uri 'none'",
			"form-action 'self'",
			"connect-src 'self' ws: wss:",
			"style-src 'self' https: 'unsafe-inline'",
			"script-src 'self'",
			"worker-src 'self'",
			"manifest-src 'self'",
			"font-src 'self' https:",
			"img-src 'self' data: https://user-images.githubusercontent.com",
		]);
		expect(response.headers.get("x-content-type-options")).toBe("nosniff");
		expect(response.headers.get("referrer-policy")).toBe("no-referrer");
	} finally {
		await new Promise<void>((resolve) => server.close(() => resolve()));
		Config.values.port = port;
		Config.values.prefetch = prefetch;
	}
});
