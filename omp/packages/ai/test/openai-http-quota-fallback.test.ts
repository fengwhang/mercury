import { expect, it } from "bun:test";
import { postOpenAIStream } from "@oh-my-pi/pi-ai/utils/openai-http";

it("surfaces exhausted quota immediately so session recovery can switch models", async () => {
	let attempts = 0;
	const controller = new AbortController();
	await expect(
		postOpenAIStream({
			url: "https://quota.test/v1/chat/completions",
			headers: {},
			body: {},
			signal: controller.signal,
			fetch: async () => {
				attempts++;
				return Response.json(
					{ error: { code: "usage_limit_reached", message: "Usage limit reached. Try again in 3600 seconds." } },
					{ status: 429, headers: { "retry-after": "3600" } },
				);
			},
		}),
	).rejects.toMatchObject({ status: 429, code: "usage_limit_reached" });
	expect(attempts).toBe(1);
});

it("retains transport retry for an ordinary request-rate throttle", async () => {
	let attempts = 0;
	const handle = await postOpenAIStream({
		url: "https://quota.test/v1/chat/completions",
		headers: {},
		body: {},
		signal: new AbortController().signal,
		fetch: async () => {
			attempts++;
			if (attempts === 1)
				return Response.json(
					{ error: { code: "rate_limit_exceeded", message: "Too many requests." } },
					{ status: 429, headers: { "retry-after-ms": "1" } },
				);
			return new Response('data: {"ok":true}\n\ndata: [DONE]\n\n', {
				headers: { "content-type": "text/event-stream" },
			});
		},
	});
	const events = [];
	for await (const event of handle.events) events.push(event);
	expect(attempts).toBe(2);
	expect(events).toEqual([{ ok: true }]);
});
