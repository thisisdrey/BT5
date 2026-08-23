### Title
Predictable repository storage path allows pre-creation collision / hijack of a future repository - (File: `internal/gitaly/repoutil/create.go`)

### Summary
The bug class in the referenced report is a `CREATE1`-style deterministic-address front-running: a resource's on-disk/on-chain location is computed from a predictable value (nonce), so an attacker can pre-occupy that location before the legitimate owner's transaction lands, and subsequent traffic intended for the legitimate resource is silently captured by the attacker's resource instead.

The closest reachable analog in Gitaly is repository creation, where the target storage path is derived from a **predictable, sequentially-reserved repository ID** rather than from any secret, and the "does it already exist" guard in `repoutil.Create` is a simple existence check rather than an ownership/content check.

### Finding Description
Repository placement on disk (for Praefect-managed / hashed storage) is computed deterministically from a repository ID: [1](#0-0) 

`DeriveReplicaPath`/`DerivePoolPath` hash the numeric repository ID and place the repo at `@cluster/repositories/<ab>/<cd>/<id>` — a location fully determined by `id` alone.

That `id` is obtained via `ReserveRepositoryID` and immediately turned into the target replica path in the router, *before* the actual repository content is written to disk: [2](#0-1) 

The actual creation on the Gitaly node then only guards against a pre-existing directory with a bare `os.Stat` before and after acquiring the repository lock — it never verifies that the pre-existing entry actually belongs to (or was created for) the reserving party: [3](#0-2) [4](#0-3) 

If any entry (directory, file, or symlink) already exists at the computed `targetPath` when `Create` runs, the function unconditionally fails with `AlreadyExists` and aborts — it does not verify that the entry was created by a legitimate, coordinated `Create` call.

### Impact Explanation
Because the destination path is a pure function of a small, monotonically-reserved integer ID (analogous to the CREATE1 nonce in the referenced report), an actor who can predict or race the next repository ID for a virtual storage can pre-create *something* at the future `DeriveReplicaPath(id)`/`DerivePoolPath(id)` location on a Gitaly storage before the legitimate `RouteRepositoryCreation` → `Create` sequence for that ID completes. When the legitimate creation subsequently runs, its `os.Stat` pre-lock/post-lock checks see the pre-existing entry and unconditionally return `AlreadyExists`, permanently denying creation of the intended repository at that ID/path (a targeted denial-of-service against a specific, predictable future repository) rather than silently redirecting traffic the way the PoolTogether vault collision did — Gitaly's check-then-act guard prevents silent hijack, but not the DoS.

### Likelihood Explanation
Exploitability hinges on whether an ordinary caller can (a) observe or predict the next `ReserveRepositoryID` value for a virtual storage, and (b) trigger creation of *some* filesystem entry at the corresponding `DeriveReplicaPath`/`DerivePoolPath` before the targeted repository's own creation call executes. `ReserveRepositoryID` reservations are performed transactionally against the datastore per creation request, so a straightforward same-ID collision through the standard `RouteRepositoryCreation` path is not directly demonstrated here — I was not able to fully trace the underlying SQL to determine whether the ID sequence is exposed/predictable enough, or whether the reservation is atomic enough to prevent two different relative paths from ever colliding on the same ID. This part of the chain (the SQL sequence semantics in `internal/praefect/datastore/repository_store.go`) could not be fully verified within the available searches.

### Recommendation
- Bind the pre-lock/post-lock existence check in `repoutil.Create` (`internal/gitaly/repoutil/create.go`) to more than bare existence — e.g., verify the pre-existing path was created by a tracked, in-flight reservation (matching repository ID/vote) rather than treating any pre-existing filesystem entry as a legitimate prior repository.
- Ensure `ReserveRepositoryID` reservations and physical path derivation happen atomically with respect to any external ability to write to storage, so no window exists between ID reservation and directory materialization where a third party could occupy the path.
- Audit whether the ID sequence backing `DeriveReplicaPath`/`DerivePoolPath` is predictable/observable by unprivileged callers, and if so, consider deriving physical paths from a value that also includes a secret/random component in addition to the numeric ID.

### Proof of Concept
Not independently reproduced end-to-end; a full PoC would require confirming that `ReserveRepositoryID` in `internal/praefect/datastore/repository_store.go` is predictable/racable by an unprivileged caller, which could not be fully verified from the code retrieved. The concrete, verified root-cause chain is: ID reservation → deterministic path derivation (`internal/gitaly/storage/repository_path.go:48-68`) → bare existence check before/after lock in `internal/gitaly/repoutil/create.go:91-104,196-228`.

### Citations

**File:** internal/gitaly/storage/repository_path.go (L45-68)
```go
// DeriveReplicaPath derives a repository's disk storage path from its repository ID. The repository ID
// is hashed with SHA256 and the first four hex digits of the hash are used as the two subdirectories to
// ensure even distribution into subdirectories. The format is @cluster/repositories/ab/cd/<repository-id>.
func DeriveReplicaPath(repositoryID int64) string {
	return deriveDiskPath(praefectRepositoryPathPrefix, repositoryID)
}

// DerivePoolPath derives an object pools's disk storage path from its repository ID. The repository ID
// is hashed with SHA256 and the first four hex digits of the hash are used as the two subdirectories to
// ensure even distribution into subdirectories. The format is @cluster/pools/ab/cd/<repository-id>. The pools
// have a different directory prefix from other repositories so Gitaly can identify them in OptimizeRepository
// and avoid pruning them.
func DerivePoolPath(repositoryID int64) string {
	return deriveDiskPath(praefectPoolPathPrefix, repositoryID)
}

func deriveDiskPath(prefixDir string, repositoryID int64) string {
	hasher := sha256.New()
	// String representation of the ID is used to make it easier to derive the replica paths with
	// external tools. The error is ignored as the hash.Hash interface is documented to never return
	// an error.
	hasher.Write([]byte(strconv.FormatInt(repositoryID, 10)))
	hash := hasher.Sum(nil)
	return filepath.Join(prefixDir, fmt.Sprintf("%x/%x/%d", hash[0:1], hash[1:2], repositoryID))
```

**File:** internal/praefect/router_per_repository.go (L474-490)
```go
	assignedNodes, err := r.assignRepositoryToNodes(virtualStorage, additionalRepoMetadata)
	if err != nil {
		return RepositoryMutatorRoute{}, err
	}

	id, err := r.rs.ReserveRepositoryID(ctx, virtualStorage, relativePath)
	if err != nil {
		return RepositoryMutatorRoute{}, fmt.Errorf("reserve repository id: %w", err)
	}

	replicaPath := storage.DeriveReplicaPath(id)
	if storage.IsRailsPoolRepository(&gitalypb.Repository{
		StorageName:  virtualStorage,
		RelativePath: relativePath,
	}) {
		replicaPath = storage.DerivePoolPath(id)
	}
```

**File:** internal/gitaly/repoutil/create.go (L91-104)
```go
	targetPath, err := locator.GetRepoPath(ctx, repository, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return structerr.NewInvalidArgument("locate repository: %w", err)
	}

	// The repository must not exist on disk already, or otherwise we won't be able to
	// create it with atomic semantics.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("pre-lock stat: %w", err)
	}
```

**File:** internal/gitaly/repoutil/create.go (L196-228)
```go

	// Now that the repository is locked, we must assert that it _still_ doesn't exist.
	// Otherwise, it could have happened that a concurrent RPC call created it while we created
	// and seeded our temporary repository. While we would notice this at the point of moving
	// the repository into place, we want to be as sure as possible that the action will succeed
	// previous to the first transactional vote.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("post-lock stat: %w", err)
	}
	if err := transaction.VoteOnContext(ctx, txManager, vote, voting.Preparing); err != nil {
		return structerr.NewFailedPrecondition("preparatory vote: %w", err)
	}

	if err := transaction.VoteOnContext(ctx, txManager, vote, voting.Prepared); err != nil {
		return structerr.NewFailedPrecondition("preparatory vote: %w", err)
	}

	syncer := safe.NewSyncer()
	if storage.NeedsSync(ctx) {
		if err := syncer.SyncRecursive(ctx, newRepoDir.Path()); err != nil {
			return fmt.Errorf("sync recursive: %w", err)
		}
	}

	// Now that we have locked the repository and all Gitalies have agreed that they
	// want to do the same change we can move the repository into place.
	if err := os.Rename(newRepoDir.Path(), targetPath); err != nil {
		return fmt.Errorf("moving repository into place: %w", err)
	}
```
