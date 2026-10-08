type MarketplaceAutoUpdateMode = "off" | "notify" | "auto";

interface MarketplaceAutoUpdateOptions {
	autoUpdate: MarketplaceAutoUpdateMode;
	resolveActiveProjectRegistryPath: (cwd: string) => Promise<string | null>;
	clearPluginRootsCache: () => void;
}

export function scheduleMarketplaceAutoUpdate(_options: MarketplaceAutoUpdateOptions): void {
	// Mercury never performs automatic registry refresh or plugin upgrades.
	// Explicit marketplace commands remain separate operator actions.
}
