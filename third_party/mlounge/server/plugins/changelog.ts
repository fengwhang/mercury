import pkg from "../../package.json";
import type ClientManager from "../clientManager";
import type {SharedChangelogData} from "../../shared/types/changelog";

const versions: SharedChangelogData = {
	current: {
		prerelease: true,
		version: `v${pkg.version}`,
		changelog: undefined,
		url: "",
	},
	expiresAt: -1,
	latest: undefined,
	packages: undefined,
};

async function fetch() {
	// Release information comes from the installed artifact, never a vendor poll.
	return versions;
}

function checkForUpdates(_manager: ClientManager) {
	// No automatic release queries or recurring vendor timers in Mercury.
}

export default {isUpdateAvailable: false, fetch, checkForUpdates};
