Based on my investigation, I've identified a solid analog in gitaly's housekeeping cooldown logic.

### Title
Full-repack cooldown bypassed via repeated object-pool link/unlink round trips causing repeated expensive full repacks - (File: internal/git/housekeeping/optimization_strategy.go)

### Summary
`HeuristicalOptimizationStrategy.ShouldRepackObjects` gates expensive full repacks behind `FullRepackCooldownPeriod` (5 days) to rate-limit CPU cost, but this cooldown is unconditionally bypassed whenever the repository's `objects/info/alternates` file has an mtime newer than the last full-repack timestamp — exactly the same "round-trip resets protective timer" pattern as the source report, where a benign transfer-out/transfer-in sequence resets a maturity/cooldown gate.

### Finding Description
The rate limiter is implemented here: [1](#0-0) . The intended bypass condition is meant to trigger a one-time full repack after a repository *newly joins* an object pool, so pool objects can be deduplicated: [2](#0-1) .

The value being compared, `Alternates.LastModified`, is simply the on-disk file mtime of `objects/info/alternates`, re-read from scratch on every optimization pass: [3](#0-2) . There is no "join generation" counter or a check for whether the *content* actually changed — only the raw mtime is compared against `LastFullRepack`.

Both `Link` and `Disconnect`/`removeAlternatesIfOk`-style flows in `internal/git/objectpool/link.go` and `internal/git/objectpool/disconnect.go` rewrite this file (via `safe.NewLockingFileWriter` on link, and via backup/rewrite on disconnect) every time they run, even when the end state is the same as before: [4](#0-3) , [5](#0-4) . `Link` does short-circuit if already linked to the *same* pool (see `linkedToRepository`), but a `Disconnect` followed by a `Link` back to the same pool is two file rewrites, each bumping the alternates mtime forward, exactly analogous to the reported "transfer to and back" pattern that reset `lastTokenTransferredTimestamp` in the NFT contract.

Because `ShouldRepackObjects` only looks at `LastModified.After(LastFullRepack)`, each such round trip forces a full repack (`RepackObjectsStrategyFullWithUnreachable`/`FullWithCruft`) on the very next housekeeping pass for that repository, regardless of how recently a full repack was already performed and regardless of `FullRepackCooldownPeriod`.

### Impact Explanation
Full repacks are the most CPU/IO-intensive housekeeping operation gitaly performs (`RepackObjectsStrategyFullWithCruft`/`FullWithUnreachable`), which is precisely why `FullRepackCooldownPeriod` exists as a resource-consumption safeguard. An actor able to trigger repeated object-pool disconnect/reconnect cycles on a repository (e.g., via toggling fork-network/pool membership operations exposed through `ObjectPoolService.LinkRepositoryToObjectPool` / `DisconnectGitAlternates`) can force gitaly to repeatedly perform full repacks on that repository well within the 5-day cooldown window, defeating the rate limiter's purpose and consuming disproportionate CPU/IO resources on the Gitaly node — a DoS on the housekeeping RPC handler's resource budget, not merely a self-inflicted inconvenience, since housekeeping runs on shared Gitaly storage nodes.

### Likelihood Explanation
Likelihood is moderate: it requires the ability to invoke the object-pool link/unlink round trip repeatedly, which in a full GitLab deployment is normally mediated by Rails business logic (e.g., visibility changes, fork network management) rather than being a raw end-user RPC call. However, within the threat model of "ordinary user driven git operations" (fork/visibility toggles causing pool relink), no additional privilege beyond normal repository ownership is required, and the underlying Gitaly-level gate provides no protection whatsoever once the alternates file is touched, so exploitation is deterministic once the round trip is achievable.

### Recommendation
Do not gate the full-repack bypass purely on raw file mtime. Instead:
1. Only trigger the "joined pool" full-repack bypass when the alternates *content* actually changes to a new pool (compare against the previously recorded object directories, not just mtime), or
2. Record a monotonic "pool join generation"/timestamp that is only bumped when a *new* alternate is added, not on every link/unlink cycle back to the same objects, and
3. Still respect `FullRepackCooldownPeriod` as an outer bound even for the "alternates changed" fast path, e.g. by capping how often this bypass condition may fire per repository.

### Proof of Concept
1. Create repository `R` and link it to object pool `P` (`LinkRepositoryToObjectPool`) — `R/objects/info/alternates` is written, `LastModified = t0`.
2. Let housekeeping perform a full repack on `R`; `LastFullRepack = t0'` (`t0' > t0`).
3. Call `DisconnectGitAlternates(R)` — objects are hard-linked back into `R`, then `objects/info/alternates` is removed/rewritten, mtime becomes `t1 > t0'`.
4. Call `LinkRepositoryToObjectPool(R, P)` again — `linkedToRepository` returns false (alternates was just removed), so the alternates file is rewritten again, mtime becomes `t2 > t1`.
5. On the very next housekeeping pass, `ShouldRepackObjects` observes `Alternates.LastModified (t2) > Packfiles.LastFullRepack (t0')` and unconditionally returns `true, fullRepackCfg`, forcing another full repack well inside `FullRepackCooldownPeriod` (5 days).
6. Repeating steps 3–4 in a loop forces a full repack on every housekeeping invocation, defeating the cooldown entirely.

### Citations

**File:** internal/git/housekeeping/optimization_strategy.go (L14-18)
```go
const (
	// FullRepackCooldownPeriod is the cooldown period that needs to pass since the last full
	// repack before we consider doing another full repack.
	FullRepackCooldownPeriod = 5 * 24 * time.Hour
)
```

**File:** internal/git/housekeeping/optimization_strategy.go (L110-116)
```go
	// When alternative object directories have been modified since our last full repack
	// then we have likely joined an object pool since then. This means that we'll want
	// to perform a full repack in order to deduplicate objects that are part of the
	// object pool.
	if s.info.Alternates.LastModified.After(s.info.Packfiles.LastFullRepack) {
		return true, fullRepackCfg
	}
```

**File:** internal/git/stats/repository_info.go (L644-668)
```go
// AlternatesInfoForRepository reads the alternates file and returns information on it. This
// function does not return an error in case the alternates file doesn't exist. Existence can be
// checked via the `Exists` field of the returned `AlternatesInfo` structure.
func AlternatesInfoForRepository(repoPath string) (AlternatesInfo, error) {
	alternatePaths, err := ReadAlternatesFile(repoPath)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return AlternatesInfo{Exists: false}, nil
		}

		return AlternatesInfo{}, fmt.Errorf("read alternates file: %w", err)
	}

	stat, err := os.Stat(AlternatesFilePath(repoPath))
	if err != nil {
		return AlternatesInfo{}, fmt.Errorf("stat: %w", err)
	}

	return AlternatesInfo{
		Exists:            true,
		ObjectDirectories: alternatePaths,
		LastModified:      stat.ModTime(),
		repoPath:          repoPath,
	}, nil
}
```

**File:** internal/git/objectpool/link.go (L28-66)
```go
func Link(ctx context.Context, pool, repo *localrepo.Repo, txManager transaction.Manager) (returnedErr error) {
	altPath, err := repo.InfoAlternatesPath(ctx)
	if err != nil {
		return err
	}

	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return err
	}

	linked, err := linkedToRepository(ctx, pool, repo)
	if err != nil {
		return err
	}

	if linked {
		// When the repository is already linked to the repository, cast a vote to ensure the
		// repository is consistent with the other replicas.
		if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("repository linked")), voting.Synchronized); err != nil {
			return fmt.Errorf("vote on linked repository: %w", err)
		}

		return nil
	}

	alternatesWriter, err := safe.NewLockingFileWriter(altPath)
	if err != nil {
		return fmt.Errorf("creating alternates writer: %w", err)
	}
	defer func() {
		if err := alternatesWriter.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("closing alternates writer: %w", err)
		}
	}()

	if _, err := io.WriteString(alternatesWriter, expectedRelPath); err != nil {
		return fmt.Errorf("writing alternates: %w", err)
	}
```

**File:** internal/git/objectpool/disconnect.go (L123-134)
```go
	altFile, err := repo.InfoAlternatesPath(ctx)
	if err != nil {
		return err
	}

	backupFile, err := newBackupFile(altFile)
	if err != nil {
		return err
	}

	return removeAlternatesIfOk(ctx, repo, altFile, backupFile, logger, txManager)
}
```
