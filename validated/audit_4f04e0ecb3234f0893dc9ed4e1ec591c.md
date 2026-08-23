I have enough evidence to write the analog finding now.

### Title
Untrusted multi-pack-index header field causes unchecked `uint64` subtraction underflow in repack heuristics - ([File: internal/git/housekeeping/optimization_strategy.go])

### Summary
`HeuristicalOptimizationStrategy.ShouldRepackObjects` computes `untrackedPackfiles := s.info.Packfiles.Count - s.info.Packfiles.MultiPackIndex.PackfileCount` as an unchecked `uint64` subtraction [1](#0-0) . `Packfiles.Count` is derived by counting `*.pack` files actually present on disk [2](#0-1) , whereas `MultiPackIndex.PackfileCount` is parsed verbatim from the 4-byte "number of packfiles" field in the `multi-pack-index` file's header, with no cross-check against the number of packfiles the MIDX actually references [3](#0-2) . This mirrors the reported bug class: an arithmetic operation assumes one operand is always ≥ the other without validating that invariant, leading to overflow/underflow.

### Finding Description
`MultiPackIndexInfoForPath` only validates the signature (`MIDX`), version (`1`), and base-file count (`0`) before trusting the 4-byte big-endian packfile count at header offset 8:12 as `PackfileCount` [3](#0-2) . It never validates that this count is consistent with the actual `PFNL`/`OIDF` chunks in the file, or that it does not exceed the number of real packfiles seen in `PackfilesInfoForRepository`. If a `multi-pack-index` file with a crafted (arbitrarily large) packfile-count header field ends up in a repository's `objects/pack` directory alongside fewer real `.pack` files, `Packfiles.Count - MultiPackIndex.PackfileCount` underflows because both are `uint64` [4](#0-3) , wrapping to a value near `2^64-1`. This wildly exceeds `actualLimit` and forces `ShouldRepackObjects` to return `true, geometricRepackCfg` on effectively every optimization decision for that repository.

Repository object-store content, including arbitrary raw files under `objects/pack/`, can be introduced from attacker-influenced sources through several ordinary write RPCs that copy or clone object data wholesale rather than validating pack structure: `CreateRepositoryFromBundle` clones directly from a client-streamed bundle into the repo [5](#0-4) , and `FetchIntoObjectPool`/`ObjectPool.FetchFromOrigin` fetches from an arbitrary origin repository and immediately triggers `OptimizeRepository` housekeeping afterward [6](#0-5) . `OptimizeRepository`/the housekeeping middleware always calls `stats.RepositoryInfoForRepository` and feeds it straight into `HeuristicalOptimizationStrategy` without sanitizing the parsed MIDX values [7](#0-6) .

### Impact Explanation
Once the underflowed `untrackedPackfiles` value is produced, every subsequent optimization pass for the affected repository is forced down the "needs geometric repack" branch, causing `git-repack` to be invoked repeatedly regardless of the repository's actual state. Because housekeeping runs automatically after mutator RPCs and object-pool fetches via the housekeeping middleware, an attacker who can get such a repository/pool created (fork, import, or fetch from an attacker-controlled origin) can force repeated, unnecessary, and potentially expensive `git-repack` executions on every housekeeping cycle — a resource-exhaustion condition on the Gitaly node (DoS of the housekeeping/repack RPC handler path), without ever violating the InvalidArgument/AlreadyExists precondition checks used elsewhere.

### Likelihood Explanation
Likelihood is moderate: the attacker doesn't need arbitrary code execution, only the ability to have Gitaly ingest a directory tree/bundle/pool source containing a crafted `multi-pack-index` file with a bogus header field and fewer real packfiles than declared. This is plausible for tenants who can control the source of a fork/import/pool-fetch operation, since none of the code paths validate the packfile count against the MIDX's own table-of-contents/`PFNL` chunk or against the real on-disk packfile count before trusting it in an unchecked unsigned subtraction.

### Recommendation
- In `MultiPackIndexInfoForPath`, cross-validate the declared packfile count against the actual number of names in the `PFNL`/`OIDF` chunks, or otherwise bound/sanitize it before returning `MultiPackIndexInfo`.
- In `HeuristicalOptimizationStrategy.ShouldRepackObjects`, replace the raw `uint64` subtraction with a saturating computation (e.g., clamp to zero if `MultiPackIndex.PackfileCount > Packfiles.Count`) so a malformed or adversarial MIDX cannot force an underflow.
- Add regression tests covering the case where `MultiPackIndex.PackfileCount` exceeds `Packfiles.Count`.

### Proof of Concept
1. Craft (or use `git multi-pack-index write` and then hand-edit) a `multi-pack-index` file whose header packfile-count field (bytes 8–11) is set to a large value (e.g., `0xFFFFFFFF`) while the `objects/pack` directory contains only 1 real `.pack` file.
2. Deliver this file into a Gitaly-managed repository via a bundle sent to `CreateRepositoryFromBundle`, or as part of an object pool source fetched by `FetchIntoObjectPool`.
3. Trigger housekeeping (automatically scheduled by the housekeeping middleware after a mutator RPC, or by calling `OptimizeRepository` directly).
4. Observe that `PackfilesInfoForRepository` reports `MultiPackIndex.PackfileCount = 0xFFFFFFFF` while `Packfiles.Count = 1`, causing `untrackedPackfiles = 1 - 0xFFFFFFFF` to underflow to a huge `uint64`, which unconditionally satisfies `untrackedPackfiles > uint64(actualLimit)` and forces a geometric repack every optimization cycle, as shown in `internal/git/housekeeping/optimization_strategy.go:206-210`.

### Citations

**File:** internal/git/housekeeping/optimization_strategy.go (L206-210)
```go
	untrackedPackfiles := s.info.Packfiles.Count - s.info.Packfiles.MultiPackIndex.PackfileCount

	if untrackedPackfiles > uint64(actualLimit) {
		return true, geometricRepackCfg
	}
```

**File:** internal/git/stats/repository_info.go (L446-459)
```go
	var info PackfilesInfo
	for _, entry := range entries {
		entryName := entry.Name()

		switch {
		case hasPrefixAndSuffix(entryName, "pack-", ".pack"):
			size, err := entrySize(entry)
			if err != nil {
				return PackfilesInfo{}, fmt.Errorf("getting packfile size: %w", err)
			}

			info.Count++
			info.Size += size

```

**File:** internal/git/stats/repository_info.go (L775-791)
```go
	version := midxHeader[4]
	if version != 1 {
		return MultiPackIndexInfo{}, fmt.Errorf("invalid version: %d", version)
	}

	baseFiles := midxHeader[7]
	if baseFiles != 0 {
		return MultiPackIndexInfo{}, fmt.Errorf("unsupported number of base files: %d", baseFiles)
	}

	packfileCount := binary.BigEndian.Uint32(midxHeader[8:12])

	midx := MultiPackIndexInfo{
		Exists:        true,
		Version:       version,
		PackfileCount: uint64(packfileCount),
	}
```

**File:** internal/gitaly/service/repository/create_repository_from_bundle.go (L37-43)
```go
	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, repo, func(repo *gitalypb.Repository) error {
		if err := s.localRepoFactory.Build(repo).CloneBundle(ctx, bundleReader); err != nil {
			return structerr.NewInternal("cloning bundle: %w", err)
		}

		return nil
	}, repoutil.WithSkipInit()); err != nil {
```

**File:** internal/git/objectpool/fetch.go (L116-135)
```go
	// This RPC fetches new objects from the origin repository into the object pool. Afterward, it
	// triggers a full set of housekeeping tasks. If WAL transaction is enabled, the housekeeping
	// manager initiates a transaction and executes all housekeeping tasks inside the transaction
	// context. Normally, the transaction life cycle is managed by a gRPC middleware. RPC handlers
	// extract the transaction from the context. Unfortunately, following that approach results in
	// two non-nested transactions. The housekeeping transaction is committed before the main
	// fetching one. The housekeeping task's effect is pushed to the next request. That's opposed to
	// the initial intention of running housekeeping after fetching. As a result, this RPC needs to
	// manage the transaction itself so that two transactions can be committed in the right order.
	if tx := storage.ExtractTransaction(ctx); tx != nil {
		commitLSN, err := tx.Commit(ctx)
		if err != nil {
			return fmt.Errorf("commit: %w", err)
		}

		storage.LogTransactionCommit(ctx, o.logger, commitLSN, "FetchFromOrigin")
	}

	// We've committed the original transaction above. OptimizeRepository internally starts
	// another transaction, and knows how to retrieve the original relative path of the repository
```

**File:** internal/git/housekeeping/manager/optimize_repository.go (L169-190)
```go
func (m *RepositoryManager) validate(
	ctx context.Context,
	repo *localrepo.Repo,
	cfg OptimizeRepositoryConfig,
) (housekeeping.OptimizationStrategy, error) {
	repositoryInfo, err := stats.RepositoryInfoForRepository(ctx, repo)
	if err != nil {
		return nil, fmt.Errorf("deriving repository info: %w", err)
	}

	repositoryInfo.Log(ctx, m.logger)
	m.metrics.ReportRepositoryInfo(repositoryInfo)

	var strategy housekeeping.OptimizationStrategy
	if cfg.StrategyConstructor == nil {
		strategy = housekeeping.NewHeuristicalOptimizationStrategy(repositoryInfo)
	} else {
		strategy = cfg.StrategyConstructor(repositoryInfo)
	}

	return strategy, nil
}
```
