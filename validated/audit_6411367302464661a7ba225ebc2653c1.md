### Title
Partition assigner merges arbitrary repositories into an attacker-influenced partition via unvalidated `objects/info/alternates` content - ([File: internal/gitaly/storage/storagemgr/partition_assigner.go])

### Summary
The reported bug class is: a state-mutating function checks a parameter against a *deny-list* (`_deprecatedGauges`) but never verifies that it is actually a member of the *allow-list* (`_gauges`), letting an out-of-scope value be silently accepted and merged into shared accounting state. In Gitaly, `partitionAssigner.getAlternatePartitionID` exhibits the same asymmetry: it only rejects an alternate path for being a *recursive* alternate or for *pointing to itself* (deny-list conditions), but never checks that the alternate actually is a legitimate, designated object pool before assigning both repositories to the same WAL partition.

### Finding Description
`getAlternatePartitionID` reads the raw `objects/info/alternates` file of a repository directly off disk and computes the alternate's relative path: [1](#0-0) 

The only rejections performed are:
- `recursiveCall` (deny-list: alternate-of-alternate chains)
- `alternateRelativePath == relativePath` (deny-list: self-reference)
- `storage.ValidateRelativePath` (containment within the storage root, not repository legitimacy)

It never verifies that `alternateRelativePath` corresponds to an actual, intentionally created object pool (e.g. via `storage.IsPoolRepository`, the check used and enforced in the higher-level `objectpool.FromProto` path): [2](#0-1) 

Note that `objectpool.FromProto` *does* perform the "allow-list" check — it requires the repository to satisfy `storage.IsPoolRepository` (or reside in the pool-creation temp dir) before treating it as a pool — but this check exists only in the higher-level `ObjectPool` construction path, not in the low-level `partitionAssigner`, which is invoked during transaction begin for every repository access: [3](#0-2) 

Once `getPartitionIDRecursive` confirms the target is merely *some* existing git directory (any repository, not necessarily a pool) via `storage.ValidateGitDirectory`, it is unconditionally folded into the same partition: [4](#0-3) 

Because partitions are the isolation/serialization boundary for Gitaly's WAL-based transaction manager (each partition shares a single write-ahead log and transactional snapshot), causing two otherwise-unrelated repositories to be assigned the same partition ID merges their transactional history and storage-manager state.

### Impact Explanation
If a repository's `objects/info/alternates` file can be made to reference an arbitrary but existing git directory within the same storage (any relative path, not just a real pool) — for example through repository import/fork/replication flows that copy repository contents including the alternates file, or any code path that writes this file without going through `objectpool.Link`'s legitimacy checks — the partition assigner will merge that unrelated repository into the same WAL partition as the attacker-influenced repository. This breaks the isolation assumption that partitions correspond to pool relationships enforced elsewhere (`IsPoolRepository`/`ObjectPool.FromProto`), potentially causing cross-repository transactional coupling, contention, or unexpected exposure of another repository's state through the shared partition, mirroring the "arbitrary reward-bearing gauge accepted because only the deny-list was checked" class of bug.

### Likelihood Explanation
The likelihood depends on how easily a user can control the raw contents of a repository's `objects/info/alternates` file outside of the sanctioned `objectpool.Link` code path (which does enforce `IsPoolRepository`/`IsValid`). I could not fully verify within the indexed code whether any user-reachable RPC (e.g. repository import, replication, or bundle/snapshot restoration) writes this file verbatim without re-validating pool legitimacy before `getAlternatePartitionID` is invoked on the next transaction. This is a genuine, confirmed validation gap in `partition_assigner.go` itself, but confirming full end-to-end reachability from an ordinary user's RPC would require additional review of the repository creation/import/replication code paths, which I was not able to completely trace with the available tools.

### Recommendation
In `getAlternatePartitionID`, before recursively assigning the alternate's partition (and thus merging partitions), validate that the alternate repository is a legitimate object pool, e.g. by calling `storage.IsPoolRepository` (or equivalent) on the resolved `alternateRelativePath`, mirroring the check already performed in `objectpool.FromProto`. Reject the alternate (return an error) if it is not a recognized pool, rather than only rejecting on self-reference/recursion.

### Proof of Concept
Not independently reproducible with the tools available in ask-only mode; the analysis is based on static code review of `internal/gitaly/storage/storagemgr/partition_assigner.go` (lines 317–360) compared against the allow-list check present in `internal/git/objectpool/pool.go` (`FromProto`, lines 46–91) and the pool-identification helper in `internal/gitaly/storage/repository_path.go` (lines 40–43). A concrete PoC would require identifying and exercising a user-reachable RPC that writes an attacker-chosen path into a repository's `objects/info/alternates` file, which I was unable to confirm exists within the indexed code coverage.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L266-288)
```go
		// With the repository under lock, verify it is a Git directory before we assign it into a partition.
		// It's okay if the repository doesn't yet exist as this transaction may be about to create it.
		if err := storage.ValidateGitDirectory(filepath.Join(pa.storagePath, relativePath)); err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				if !isRepositoryCreation {
					return 0, relativePathNotFoundError(relativePath)
				}

				// Repository creations are allowed to target non-existing repositories. They create the partition
				// where the repository is to be created.
			} else {
				return 0, fmt.Errorf("validate git directory: %w", err)
			}
		}

		ptnID, err = pa.assignPartitionID(ctx, relativePath, recursiveCall, partitionHint)
		if err != nil {
			return 0, fmt.Errorf("assign partition ID: %w", err)
		}
	}

	return ptnID, nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L317-349)
```go
func (pa *partitionAssigner) getAlternatePartitionID(ctx context.Context, relativePath string, recursiveCall bool, partitionHint storage.PartitionID) (storage.PartitionID, error) {
	alternate, err := gitstorage.ReadAlternatesFile(filepath.Join(pa.storagePath, relativePath))
	if err != nil {
		return 0, fmt.Errorf("read alternates file: %w", err)
	}

	if recursiveCall {
		// recursive being true indicates we've arrived here through another repository's alternate.
		// Repositories in Gitaly should only have a single alternate that points to the repository's
		// pool. Chains of alternates are unexpected and could go arbitrarily long, so fail the operation.
		return 0, storage.ErrAlternateHasAlternate
	}

	// The relative path should point somewhere within the same storage.
	alternateRelativePath, err := storage.ValidateRelativePath(
		pa.storagePath,
		// Take the relative path to the repository, not 'repository/objects'.
		filepath.Dir(
			// The path in alternates file points to the object directory of the alternate
			// repository. The path is relative to the repository's own object directory.
			filepath.Join(relativePath, "objects", alternate),
		),
	)
	if err != nil {
		return 0, fmt.Errorf("validate relative path: %w", err)
	}

	if alternateRelativePath == relativePath {
		// The alternate must not point to the repository itself. Not only is it non-sensical
		// but it would also cause a dead lock as the repository is locked during this call
		// already.
		return 0, storage.ErrAlternatePointsToSelf
	}
```

**File:** internal/git/objectpool/pool.go (L46-91)
```go
// FromProto returns an object pool object from its Protobuf representation. This function verifies
// that the object pool exists and is a valid pool repository.
func FromProto(
	ctx context.Context,
	logger log.Logger,
	locator storage.Locator,
	gitCmdFactory gitcmd.CommandFactory,
	catfileCache catfile.Cache,
	txManager transaction.Manager,
	housekeepingManager housekeepingmgr.Manager,
	proto *gitalypb.ObjectPool,
) (*ObjectPool, error) {
	poolPath, err := locator.GetRepoPath(ctx, proto.GetRepository(), storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	if !storage.IsPoolRepository(proto.GetRepository()) {
		// When creating repositories in the ObjectPool service we will first create the
		// repository in a temporary directory. So we need to check whether the path we see
		// here is in such a temporary directory and let it pass.
		tempDir, err := locator.TempDir(proto.GetRepository().GetStorageName())
		if err != nil {
			return nil, fmt.Errorf("getting temporary storage directory: %w", err)
		}

		if !strings.HasPrefix(poolPath, tempDir) {
			return nil, ErrInvalidPoolDir
		}
	}

	pool := &ObjectPool{
		Repo:                localrepo.New(logger, locator, gitCmdFactory, catfileCache, proto.GetRepository()),
		logger:              logger,
		locator:             locator,
		gitCmdFactory:       gitCmdFactory,
		txManager:           txManager,
		housekeepingManager: housekeepingManager,
	}

	if !pool.IsValid(ctx) {
		return nil, ErrInvalidPoolRepository
	}

	return pool, nil
}
```

**File:** internal/gitaly/storage/repository_path.go (L40-43)
```go
// IsPoolRepository returns whether the repository is an object pool.
func IsPoolRepository(repo Repository) bool {
	return IsRailsPoolRepository(repo) || IsPraefectPoolRepository(repo)
}
```
