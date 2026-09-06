import { Settings } from "./src/config/settings";
import { ModelRegistry } from "./src/config/model-registry";
import { resolveAdvisorRoleSelection } from "./src/config/model-resolver";
import { AuthStorage } from "./src/session/auth-storage";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const settings = Settings.isolated({ "compaction.enabled": false, "retry.enabled": false });
const authStorage = await AuthStorage.create(":memory:");
authStorage.setRuntimeApiKey("anthropic", "test-key");
const dir = mkdtempSync(join(tmpdir(), "adv-probe-"));
const modelRegistry = new ModelRegistry(authStorage, join(dir, "models.yml"));
const available = modelRegistry.getAvailable();
console.log("available count:", available.length);
console.log("claude-sonnet present:", available.some(m => m.id.includes("claude-sonnet-4-5")));
const sel = resolveAdvisorRoleSelection(settings, available);
console.log("resolveAdvisorRoleSelection ->", sel ? sel.model.id : "undefined");
