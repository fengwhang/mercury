/**
 * `--isolate-worktree` backing: create a fresh linked git worktree on a new
 * `omp-isolated/<slug>` branch (from `HEAD`) under the repo's
 * `.omp/worktrees/` dir, so the session can start isolated from the active
 * checkout.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as vcs from "@oh-my-pi/pi-natives/vcs";
import { generateTaskName } from "../task/name-generator";

export interface TopLevelWorktree {
	/** Absolute, realpath'd worktree root. */
	path: string;
	/** Branch checked out in the worktree (created from `HEAD`). */
	branch: string;
}

/** Worktree branch prefix for `--isolate-worktree` sessions. */
const ISOLATED_BRANCH_PREFIX = "omp-isolated";

/** `.gitignore` entry keeping isolated worktrees untracked. */
const WORKTREES_GITIGNORE_ENTRY = ".omp/worktrees/";

/**
 * Create the worktree for `--isolate-worktree`. Throws with a user-facing
 * message when `cwd` is not a git checkout, the name is not a valid branch
 * slug, or the branch / directory already exists.
 */
export async function createTopLevelWorktree(cwd: string, name?: string): Promise<TopLevelWorktree> {
	const repository = vcs.git(cwd);
	if (!repository) {
		throw new Error(`Not inside a git repository: ${cwd}`);
	}
	const rawName = name ?? generateTaskName();
	const slug = rawName.replaceAll(/[^A-Za-z0-9._-]+/g, "-");
	if (slug.length === 0 || slug.startsWith(".") || slug.startsWith("-") || slug.endsWith("/") || slug.includes("..")) {
		throw new Error(`Invalid worktree name: ${rawName}`);
	}
	const branch = `${ISOLATED_BRANCH_PREFIX}/${slug}`;
	const branchRef = `refs/heads/${branch}`;
	if (await repository.refExists(branchRef)) {
		throw new Error(`Branch '${branch}' already exists; pick another name.`);
	}
	const repoRoot = repository.primaryRoot() ?? repository.info().repoRoot;
	const worktreePath = path.join(repoRoot, ".omp", "worktrees", slug);
	try {
		await fs.stat(worktreePath);
		throw new Error(`Worktree path '${worktreePath}' already exists; pick another name.`);
	} catch (error) {
		if (error instanceof Error && error.message.startsWith("Worktree path")) {
			throw error;
		}
		// Missing path is the expected case — proceed to create the worktree.
	}
	await fs.mkdir(path.dirname(worktreePath), { recursive: true });

	await repository.createBranch(branch, "HEAD", false);
	await repository.worktreeAdd(worktreePath, branch, { detach: false, clone: false });

	await ensureGitignored(repoRoot);

	return {
		path: await fs.realpath(worktreePath),
		branch,
	};
}

/**
 * Best-effort: keep isolated worktrees untracked. Never throws — a missing or
 * read-only `.gitignore` must not block isolation.
 */
async function ensureGitignored(repoRoot: string): Promise<void> {
	try {
		const gitignorePath = path.join(repoRoot, ".gitignore");
		const file = Bun.file(gitignorePath);
		if (await file.exists()) {
			const current = await file.text();
			const lines = current.split("\n").map(line => line.trim());
			if (lines.includes(WORKTREES_GITIGNORE_ENTRY) || lines.includes(".omp/worktrees")) {
				return;
			}
			const prefix = current.length > 0 && !current.endsWith("\n") ? "\n" : "";
			await Bun.write(gitignorePath, `${current}${prefix}${WORKTREES_GITIGNORE_ENTRY}\n`);
		} else {
			await Bun.write(gitignorePath, `${WORKTREES_GITIGNORE_ENTRY}\n`);
		}
	} catch {
		// Best-effort only; isolation succeeds without the ignore entry.
	}
}
