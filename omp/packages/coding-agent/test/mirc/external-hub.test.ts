import { afterEach, expect, test } from "bun:test";
import { AgentRegistry } from "../../src/registry/agent-registry";
import { MircBus } from "../../src/mirc/bus";
import type { AgentSession } from "../../src/session/agent-session";

const cleanup: Array<() => Promise<void> | void> = [];
afterEach(async () => { for (const close of cleanup.splice(0).reverse()) await close(); });

// Separate registries are the actual Hermes spawn boundary: each external
// child's native Main starts in a separate process. No provider is involved.
test("Hermes OMP siblings have native mailbox delivery across process registries", async () => {
  const left = new AgentRegistry();
  const right = new AgentRegistry();
  const received: string[] = [];
  right.register({ id: "Right", displayName: "Right", kind: "sub", parentId: "Main",
    session: { deliverMircMessage: async (msg: { body: string }) => { received.push(msg.body); return "injected"; } } as unknown as AgentSession });
  let bus = new MircBus(left);
  const adapter = "../../src/mirc/external-hub";
  if (await Bun.file(new URL("../../src/mirc/external-hub.ts", import.meta.url)).exists()) {
    const { NativeHubServer, HubScopeClient } = await import(adapter);
    const server = await NativeHubServer.start(); cleanup.push(() => server.close());
    const l = await HubScopeClient.connect(server.address, server.issue("Left", "Main"), left);
    const r = await HubScopeClient.connect(server.address, server.issue("Right", "Main"), right);
    cleanup.push(() => l.close(), () => r.close());
    left.register({ id: "Left", displayName: "Left", kind: "sub", parentId: "Main", session: null });
    await l.flush(); await r.flush();
    bus = l.bus;
  }
  const receipt = await bus.send({ from: "Left", to: "Right", body: "sibling-roundtrip" });
  expect(receipt.outcome).toBe("injected");
  expect(received).toEqual(["sibling-roundtrip"]);
}, 10000);
