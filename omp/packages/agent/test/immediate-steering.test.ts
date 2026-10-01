import { describe, expect, it } from "bun:test";
import { type } from "@oh-my-pi/omptype";
import { Agent, type AgentEvent, type AgentTool } from "@oh-my-pi/pi-agent-core";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { createAssistantMessage, createUserMessage } from "./helpers";

describe("immediate user steering", () => {
	it("interrupts stalled model output and resumes without ending the RPC run", async () => {
		const mock = createMockModel({ responses: [{ content: ["changed direction"] }] });
		const partial = Promise.withResolvers<void>();
		const events: AgentEvent[] = [];
		let requestSignal: AbortSignal | undefined;
		let calls = 0;
		const agent = new Agent({
			initialState: { model: mock.model },
			streamFn: (model, context, options) => {
				if (++calls > 1) return mock.stream(model, context, options);
				requestSignal = options?.signal;
				const stream = new AssistantMessageEventStream();
				stream.push({
					type: "start",
					partial: createAssistantMessage([{ type: "text", text: "unfinished reply" }]),
				});
				// Deliberately never ends, even on abort: cancellation must race the iterator.
				return stream;
			},
		});
		agent.subscribe(event => {
			events.push(event);
			if (event.type === "message_start" && event.message.role === "assistant") partial.resolve();
		});
		const run = agent.prompt("original direction");
		await partial.promise;
		agent.steer(createUserMessage("change direction now"));
		await run;
		expect(requestSignal?.aborted).toBe(true);
		expect(calls).toBe(2);
		expect(
			mock.calls[0].context.messages.some(
				message => message.role === "user" && message.content === "change direction now",
			),
		).toBe(true);
		expect(events.filter(event => event.type === "agent_end")).toHaveLength(1);
		expect(events.filter(event => event.type === "turn_end")).toHaveLength(2);
		expect(agent.hasQueuedMessages()).toBe(false);
	});

	it("resumes if steering cancels the provider before response headers arrive", async () => {
		const mock = createMockModel({ responses: [{ content: ["revised"] }] });
		const started = Promise.withResolvers<void>();
		let calls = 0;
		const agent = new Agent({
			initialState: { model: mock.model },
			streamFn: async (model, context, options) => {
				if (++calls > 1) return mock.stream(model, context, options);
				return await new Promise<AssistantMessageEventStream>((_resolve, reject) => {
					options?.signal?.addEventListener("abort", () => reject(new Error("request cancelled")), { once: true });
					started.resolve();
				});
			},
		});
		const run = agent.prompt("original");
		await started.promise;
		agent.steer(createUserMessage("new direction"));
		await run;
		expect(calls).toBe(2);
		expect(agent.state.messages.at(-1)).toMatchObject({ role: "assistant", stopReason: "stop" });
	});

	it("resumes when the provider emits its abort error before the cancellation race", async () => {
		const mock = createMockModel({ responses: [{ content: ["revised"] }] });
		const started = Promise.withResolvers<void>();
		let calls = 0;
		const agent = new Agent({
			initialState: { model: mock.model },
			streamFn: (model, context, options) => {
				if (++calls > 1) return mock.stream(model, context, options);
				const stream = new AssistantMessageEventStream();
				const partial = createAssistantMessage([{ type: "text", text: "unfinished" }]);
				stream.push({ type: "start", partial });
				options?.signal?.addEventListener(
					"abort",
					() => {
						const error = { ...partial, stopReason: "aborted" as const, errorMessage: "provider cancelled" };
						stream.push({ type: "error", reason: "aborted", error });
						stream.end(error);
					},
					{ once: true },
				);
				return stream;
			},
		});
		agent.subscribe(event => {
			if (event.type === "message_start" && event.message.role === "assistant") started.resolve();
		});
		const run = agent.prompt("original");
		await started.promise;
		agent.steer(createUserMessage("revised"));
		await run;
		expect(calls).toBe(2);
		expect(agent.state.messages.at(-1)).toMatchObject({ role: "assistant", stopReason: "stop" });
	});

	for (const { mode, interruptible } of [
		{ mode: "immediate", interruptible: true },
		{ mode: "immediate", interruptible: false },
		{ mode: "wait", interruptible: true },
	] as const) {
		it(`${mode} steering ${interruptible && mode === "immediate" ? "interrupts pure waits" : "preserves running work"}`, async () => {
			const started = Promise.withResolvers<void>();
			const finish = Promise.withResolvers<void>();
			const schema = type({ value: "string" });
			const executed: string[] = [];
			let toolSignal: AbortSignal | undefined;
			const tool: AgentTool<typeof schema> = {
				name: "work",
				label: "Work",
				description: "Foreground work",
				parameters: schema,
				concurrency: "exclusive",
				interruptible,
				async execute(_id, args, signal) {
					executed.push(args.value);
					toolSignal = signal;
					if (args.value === "first") {
						const cancelled = new Promise<never>((_resolve, reject) =>
							signal?.addEventListener("abort", () => reject(new Error("cancelled")), { once: true }),
						);
						started.resolve();
						await Promise.race([finish.promise, cancelled]);
					}
					return { content: [{ type: "text", text: args.value }], details: {} };
				},
			};
			const mock = createMockModel({
				responses: [
					{
						content: [
							{ type: "toolCall", id: "first", name: "work", arguments: { value: "first" } },
							{ type: "toolCall", id: "second", name: "work", arguments: { value: "second" } },
						],
						stopReason: "toolUse",
					},
					{ content: ["revised"] },
				],
			});
			const agent = new Agent({
				initialState: { model: mock.model, tools: [tool] },
				streamFn: mock.stream,
				interruptMode: mode,
			});
			const run = agent.prompt("start");
			await started.promise;
			agent.steer(createUserMessage("change direction"));
			if (mode === "wait" || !interruptible) {
				expect(toolSignal?.aborted).toBe(false);
				finish.resolve();
			}
			await run;
			expect(executed).toEqual(mode === "immediate" && interruptible ? ["first"] : ["first", "second"]);
			expect(mock.calls[1].context.messages.filter(message => message.role === "toolResult")).toHaveLength(2);
			expect(agent.hasQueuedMessages()).toBe(false);
		});
	}
});
