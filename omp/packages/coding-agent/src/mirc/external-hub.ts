/** Local external-parent transport for the EXISTING native mailbox bus.
 * No IRC daemon, hosted service, Observatory room, transcript path or provider.
 * Each connection receives a bounded subtree grant, not the owner's credential.
 */
import { randomBytes } from "node:crypto";
import { createConnection, createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { type } from "@oh-my-pi/omptype";
import { Settings } from "../config/settings";
import { AgentRegistry } from "../registry/agent-registry";
import type { AgentRef } from "../registry/agent-registry";
import type { AgentSession } from "../session/agent-session";
import { executeList, executeMessageWait, executeSend } from "../tools/hub/messaging";
import { MircBus } from "./bus";
import type { MircDeliveryReceipt, MircMessage, MircSendOptions } from "./bus";

type WireRef = Pick<AgentRef, "id" | "displayName" | "kind" | "parentId" | "status" | "lastActivity" | "activity">;
type Frame = { id?: number; method?: string; data?: unknown; result?: unknown; error?: string };
const frameSchema = type.declare<Frame>().type({ "id?": "number", "method?": "string", "data?": "unknown", "result?": "unknown", "error?": "string" });
const refSchema = type.declare<WireRef>().type({
  id: "string", displayName: "string", kind: "'main'|'sub'|'advisor'", "parentId?": "string",
  status: "'running'|'idle'|'parked'|'aborted'", lastActivity: "number", "activity?": "string",
});
const refsSchema = refSchema.array();
const messageSchema = type.declare<MircMessage>().type({
  id: "string", from: "string", to: "string", body: "string", ts: "number", "replyTo?": "string",
});
const optionsSchema = type.declare<MircSendOptions>().type({ "expectsReply?": "boolean", "suppressRelay?": "boolean" });
const receiptSchema = type.declare<MircDeliveryReceipt>().type({ to: "string", outcome: "'injected'|'woken'|'revived'|'failed'", "error?": "string" });
const deliverSchema = type({ message: messageSchema, "options?": optionsSchema });
const sendSchema = type({ message: messageSchema.omit("id", "ts"), "options?": optionsSchema });
const identitySchema = type({ root: "string", "parent?": "string" });
const helloSchema = type({ root: "string", "parent?": "string", refs: refsSchema });
const peerToolSchema = type({
  op: "'list'|'send'|'wait'", "to?": "string", "message?": "string", "replyTo?": "string",
  "await?": "boolean", "timeoutMs?": "number", "from?": "string", "status?": "'running'|'idle'|'parked'", "limit?": "number",
});
const FRAME_LIMIT = 1024 * 1024;
const REQUEST_TIMEOUT = 10000;

/** Correlated duplex frames. Disconnect rejects, never replays accepted sends. */
class PeerWire {
  #serial = 0;
  #pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();
  #buffer = "";
  handler: (method: string, data: unknown) => Promise<unknown> = async () => { throw new Error("Hub not initialized"); };
  onClose: () => void = () => {};
  constructor(readonly socket: Socket) {
    socket.setEncoding("utf8");
    socket.on("data", chunk => {
      this.#buffer += chunk;
      if (Buffer.byteLength(this.#buffer) > FRAME_LIMIT) { socket.destroy(); return; }
      let newline: number;
      while ((newline = this.#buffer.indexOf("\n")) >= 0) {
        const line = this.#buffer.slice(0, newline); this.#buffer = this.#buffer.slice(newline + 1);
        let frame: Frame;
        try { frame = frameSchema.assert(JSON.parse(line)); } catch { socket.destroy(); return; }
        if (frame.method) {
          void this.handler(frame.method, frame.data).then(
            result => this.#write({ id: frame.id, result }),
            error => this.#write({ id: frame.id, error: error instanceof Error ? error.message : String(error) }),
          );
        } else if (frame.id !== undefined) {
          const pending = this.#pending.get(frame.id);
          if (!pending) continue;
          this.#pending.delete(frame.id); clearTimeout(pending.timer);
          if (frame.error) pending.reject(new Error(frame.error)); else pending.resolve(frame.result);
        }
      }
    });
    socket.on("error", () => {});
    socket.on("close", () => {
      for (const pending of this.#pending.values()) { clearTimeout(pending.timer); pending.reject(new Error("Hub connection closed")); }
      this.#pending.clear(); this.onClose();
    });
  }
  #write(frame: Frame): void {
    if (this.socket.destroyed) return;
    const encoded = JSON.stringify(frame);
    if (Buffer.byteLength(encoded) > FRAME_LIMIT) throw new Error("Hub frame too large");
    this.socket.write(`${encoded}\n`);
  }
  request(method: string, data: unknown, timeout = REQUEST_TIMEOUT): Promise<unknown> {
    if (this.socket.destroyed) return Promise.reject(new Error("Hub connection closed"));
    const id = ++this.#serial;
    const { promise, resolve, reject } = Promise.withResolvers<unknown>();
    const timer = setTimeout(() => { this.#pending.delete(id); reject(new Error("Hub request timed out; send not replayed")); }, timeout);
    timer.unref(); this.#pending.set(id, { resolve, reject, timer });
    try { this.#write({ id, method, data }); }
    catch (error) { clearTimeout(timer); this.#pending.delete(id); reject(error); }
    return promise;
  }
  close(): void { this.socket.destroy(); }
}

function publicRef(ref: AgentRef): WireRef {
  return { id: ref.id, displayName: ref.displayName, kind: ref.kind, parentId: ref.parentId,
    status: ref.status, lastActivity: ref.lastActivity, activity: ref.activity };
}

type Grant = { root: string; parent?: string; admin: boolean; wire?: PeerWire; refs: Set<string> };
export class NativeHubServer {
  readonly registry = new AgentRegistry();
  readonly bus = new MircBus(this.registry, undefined, async (message, options) => {
    const owner = this.#owners.get(message.to);
    if (!owner?.wire) return { to: message.to, outcome: "failed", error: "Peer connection closed" };
    try { return receiptSchema.assert(await owner.wire.request("deliver", { message, options })); }
    catch (error) { return { to: message.to, outcome: "failed", error: String(error) }; }
  });
  readonly #grants = new Map<string, Grant>();
  readonly #owners = new Map<string, Grant>();
  readonly #wires = new Set<PeerWire>();
  readonly #settings = Settings.isolated();
  #server!: Server;
  address!: string;
  readonly ownerToken: string;
  constructor() { this.ownerToken = this.issue("Main", undefined, true); }
  issue(root: string, parent?: string, admin = false): string {
    if (!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$/.test(root)) throw new Error("Invalid hub identity");
    if ([...this.#grants.values()].some(grant => grant.root === root)) throw new Error("Hub identity already granted");
    const token = randomBytes(32).toString("hex");
    this.#grants.set(token, { root, parent, admin, refs: new Set() });
    return token;
  }
  static async start(): Promise<NativeHubServer> {
    const scope = new NativeHubServer();
    scope.#server = createServer(socket => scope.#accept(new PeerWire(socket)));
    const { promise, resolve, reject } = Promise.withResolvers<void>();
    scope.#server.once("error", reject); scope.#server.listen(0, "127.0.0.1", resolve);
    await promise;
    const addr = scope.#server.address();
    if (!addr || typeof addr === "string") throw new Error("Hub address unavailable");
    scope.address = `127.0.0.1:${addr.port}`;
    return scope;
  }
  #snapshot(): WireRef[] { return this.registry.list().map(publicRef); }
  #broadcastRoster(): void {
    const refs = this.#snapshot();
    for (const wire of this.#wires) void wire.request("roster", refs).catch(() => {});
  }
  #accept(wire: PeerWire): void {
    let grant: Grant | undefined;
    const authTimer = setTimeout(() => wire.close(), REQUEST_TIMEOUT); authTimer.unref();
    wire.onClose = () => {
      clearTimeout(authTimer); this.#wires.delete(wire);
      if (grant?.wire !== wire) return; // old connection cannot remove its replacement
      grant.wire = undefined;
      for (const id of grant.refs) { this.registry.unregister(id); this.#owners.delete(id); }
      grant.refs.clear(); this.#broadcastRoster();
    };
    wire.handler = async (method, data) => {
      if (method === "hello") {
        if (grant) throw new Error("Already authenticated");
        grant = this.#grants.get(type({ token: "string" }).assert(data).token);
        if (!grant) throw new Error("Invalid hub capability");
        clearTimeout(authTimer);
        const prior = grant.wire; grant.wire = wire; prior?.close();
        this.#wires.add(wire);
        return { root: grant.root, parent: grant.parent, refs: this.#snapshot() };
      }
      if (!grant || grant.wire !== wire) throw new Error("Hub capability required");
      if (method === "grant") {
        if (!grant.admin) throw new Error("Owner capability required");
        const identity = identitySchema.assert(data);
        return this.issue(identity.root, identity.parent);
      }
      if (method === "register") {
        const ref = refSchema.assert(data);
        if (ref.id === grant.root) {
          if (ref.parentId !== grant.parent || ref.kind !== (grant.admin ? "main" : "sub")) throw new Error("Root topology mismatch");
        } else if (!ref.parentId || this.#owners.get(ref.parentId) !== grant || ref.kind === "main") throw new Error("Subtree capability required");
        const owner = this.#owners.get(ref.id);
        if (owner && owner !== grant) throw new Error("Peer identity owned by another subtree");
        // Terminal tombstones may not be resurrected by a stale registration.
        if (this.registry.get(ref.id)?.status === "aborted" && ref.status !== "aborted") throw new Error("Aborted peer is terminal");
        const proxy = { isStreaming: ref.status === "running", subscribe: () => () => {}, waitForMircReplies: async () => {},
          deliverMircMessage: async () => "injected", emitMircRelayObservation: () => {} } as unknown as AgentSession;
        this.registry.register({ ...ref, session: proxy });
        this.#owners.set(ref.id, grant); grant.refs.add(ref.id); this.#broadcastRoster(); return {};
      }
      if (method === "remove") {
        const { id } = type({ id: "string" }).assert(data);
        if (this.#owners.get(id) !== grant) throw new Error("Subtree capability required");
        this.registry.unregister(id); this.#owners.delete(id); grant.refs.delete(id); this.#broadcastRoster(); return {};
      }
      if (method === "snapshot") return this.#snapshot();
      if (method === "send") {
        const payload = sendSchema.assert(data);
        if (this.#owners.get(payload.message.from) !== grant) throw new Error("Sender capability required");
        return this.bus.send(payload.message, payload.options);
      }
      if (method === "tool") {
        const params = peerToolSchema.assert(data);
        const senderId = grant.root;
        const deps = { registry: this.registry, senderId, settings: this.#settings, bus: this.bus };
        if (params.op === "list") return executeList(this.registry, senderId, params);
        if (params.op === "send") return executeSend(deps, params);
        if (params.op === "wait") return executeMessageWait(deps, params);
        throw new Error("Only native peer list/send/wait are forwarded");
      }
      throw new Error("Unknown hub method");
    };
  }
  async close(): Promise<void> {
    for (const wire of this.#wires) wire.close();
    const { promise, resolve } = Promise.withResolvers<void>();
    this.#server.close(() => resolve()); await promise;
    this.#grants.clear(); this.#owners.clear();
  }
}

/** Connects one external OMP subtree. Native registry/session lifecycle remains local. */
export class HubScopeClient {
  readonly bus: MircBus;
  readonly #mirrors = new Set<string>();
  readonly #owned = new Set<string>();
  #wire!: PeerWire;
  #unsubscribe?: () => void;
  #publishing = Promise.resolve();
  #syncing = false;
  #closed = false;
  root!: string;
  parent?: string;
  private constructor(readonly registry: AgentRegistry, bus?: MircBus) { this.bus = bus ?? new MircBus(registry); }
  static async connect(address: string, token: string, registry = AgentRegistry.global(), bus?: MircBus): Promise<HubScopeClient> {
    if (!/^127\.0\.0\.1:\d+$/.test(address)) throw new Error("Hub transport must be localhost");
    const client = new HubScopeClient(registry, bus);
    const socket = createConnection({ host: "127.0.0.1", port: Number(address.split(":")[1]) });
    client.#wire = new PeerWire(socket);
    client.#wire.handler = async (method, data) => {
      if (method === "roster") { client.#roster(refsSchema.assert(data)); return {}; }
      if (method === "deliver") {
        const payload = deliverSchema.assert(data);
        return client.bus.receive(payload.message, payload.options);
      }
      throw new Error("Unknown hub callback");
    };
    try {
      const hello = helloSchema.assert(await client.#wire.request("hello", { token }));
      client.root = hello.root; client.parent = hello.parent;
      client.#roster(hello.refs);
      client.bus.setTransport(async (message, options) => {
        await client.flush();
        return receiptSchema.assert(await client.#wire.request("send", { message, options }));
      });
      client.#unsubscribe = registry.onChange(event => {
        if (client.#syncing || client.#mirrors.has(event.ref.id)) return;
        client.#owned.add(event.ref.id);
        client.#publishing = client.#publishing.then(async () => {
          if (client.#closed) return;
          if (event.type === "removed") { await client.#wire.request("remove", { id: event.ref.id }); client.#owned.delete(event.ref.id); }
          else await client.#wire.request("register", publicRef(event.ref));
        });
        // Retain the rejection for flush callers; prevent unhandled task noise.
        void client.#publishing.catch(() => {});
      });
      for (const ref of registry.list()) {
        if (client.#mirrors.has(ref.id)) continue;
        client.#owned.add(ref.id); await client.#wire.request("register", publicRef(ref));
      }
      return client;
    } catch (error) { client.close(); throw error; }
  }
  #roster(refs: WireRef[]): void {
    this.#syncing = true;
    try {
      const incoming = new Set(refs.map(ref => ref.id));
      for (const id of this.#mirrors) if (!incoming.has(id)) { this.registry.unregister(id); this.#mirrors.delete(id); }
      for (const ref of refs) {
        if (this.#owned.has(ref.id)) continue;
        // Do not replace locally constructed sessions, including during hello.
        if (this.registry.get(ref.id) && !this.#mirrors.has(ref.id)) continue;
        this.#mirrors.add(ref.id);
        this.registry.register({ ...ref, session: { isStreaming: ref.status === "running" } as AgentSession });
      }
    } finally { this.#syncing = false; }
  }
  async flush(): Promise<void> { await this.#publishing; this.#roster(refsSchema.assert(await this.#wire.request("snapshot", {}))); }
  async publish(ref: AgentRef): Promise<void> {
    this.#owned.add(ref.id); this.#mirrors.delete(ref.id);
    await this.#wire.request("register", publicRef(ref));
  }
  close(): void {
    if (this.#closed) return;
    this.#closed = true; this.#unsubscribe?.(); this.bus.setTransport(); this.#wire?.close();
    this.#syncing = true;
    for (const id of this.#mirrors) this.registry.unregister(id);
    this.#mirrors.clear(); this.#syncing = false;
  }
}

/** Distribution entry: parent receives only an ephemeral localhost address/token. */
export async function runNativeHubServer(): Promise<void> {
  const server = await NativeHubServer.start();
  process.stdout.write(`${JSON.stringify({ address: server.address, token: server.ownerToken })}\n`);
  process.stdin.resume();
  process.stdin.once("end", () => { void server.close().then(() => process.exit(0)); });
  process.once("SIGTERM", () => { void server.close().then(() => process.exit(0)); });
}
