// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebhooks: vi.fn(), enableWebhooks: vi.fn(), restartGateway: vi.fn(),
  getActionStatus: vi.fn(), showToast: vi.fn(), setEnd: vi.fn(),
}));
vi.mock("@/lib/api", () => ({ api: mocks }));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: mocks.setEnd }),
}));
vi.mock("@nous-research/ui/hooks/use-toast", () => ({
  useToast: () => ({ toast: null, showToast: mocks.showToast }),
}));

import WebhooksPage from "./WebhooksPage";

let root: Root;
let container: HTMLDivElement;
beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  mocks.getWebhooks.mockResolvedValue({ enabled: false, subscriptions: [], base_url: "" });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
});

async function enable() {
  await act(async () => root.render(<WebhooksPage />));
  const button = Array.from(container.querySelectorAll("button"))
    .find((element) => element.textContent?.includes("Enable webhooks"));
  expect(button).toBeDefined();
  await act(async () => button!.click());
}

it("shows queued busy deferral without a failed-restart banner or admin fallback", async () => {
  mocks.enableWebhooks.mockResolvedValue({
    ok: true, enabled: true, needs_restart: true, restart_started: false,
    restart_queued: true, restart_deferred: true, restart_pid: 4242,
  });
  await enable();
  expect(container.textContent).toContain("deferred");
  expect(container.textContent).not.toContain("Gateway restart failed");
  expect(mocks.restartGateway).not.toHaveBeenCalled();
  expect(mocks.getActionStatus).not.toHaveBeenCalled();
});

it("does not poll a phantom admin action for an admitted automatic restart", async () => {
  mocks.enableWebhooks.mockResolvedValue({
    ok: true, enabled: true, needs_restart: false, restart_started: true,
    restart_queued: true, restart_deferred: false, restart_pid: 4242,
  });
  vi.useFakeTimers();
  await enable();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(mocks.getActionStatus).not.toHaveBeenCalled();
  expect(mocks.restartGateway).not.toHaveBeenCalled();
});
