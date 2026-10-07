import { expect, it } from "bun:test";
import { makeOpenRouterRewriter } from "./rewrite-system-prompt";

it("rewrites through an explicitly selected endpoint with Mercury attribution and no implicit website", async () => {
	let headers: Headers | undefined;
	let body: unknown;
	const server = Bun.serve({
		hostname: "127.0.0.1",
		port: 0,
		async fetch(request) {
			headers = request.headers;
			body = await request.json();
			return Response.json({ choices: [{ message: { content: '{"items":[{"id":1,"text":"Keep `token`."}]}' } }] });
		},
	});
	try {
		const rewrite = makeOpenRouterRewriter({
			apiKey: "test-key",
			model: "test-model",
			baseUrl: `http://127.0.0.1:${server.port}`,
			temperature: 0,
			retries: 0,
			system: "Test-only instruction.",
		});
		const result = await rewrite([{ id: 1, text: "Please keep `token`.", tokens: ["`token`"] }]);
		expect(result.get(1)).toBe("Keep `token`.");
		if (!headers) throw new Error("Expected a request to the local endpoint");
		expect(headers.has("HTTP-Referer")).toBe(false);
		expect(headers.get("X-Title")).toBe("Mercury");
		expect(headers.get("Authorization")).toBe("Bearer test-key");
		expect(headers.get("Content-Type")).toBe("application/json");
		expect(body).toMatchObject({ model: "test-model", messages: [{ role: "system" }, { role: "user" }] });
	} finally {
		server.stop(true);
	}
});
