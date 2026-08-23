### Title
`StorePoolMetadata` accepts unvalidated repository-to-pool associations from any authenticated caller, corrupting object-pool accounting - (File: internal/gitaly/service/internalgitaly/store_pool_metadata.go)

### Summary
`InternalGitaly.StorePoolMetadata` writes attacker/caller-supplied `(storage_name, relative_path, pool_disk_path, is_upstream)` tuples directly into the pool-metadata database with no verification that the referenced repositories exist, that the named pool is actually a valid object pool, or that the member repository actually references that pool via its `objects/info/alternates` file. This mirrors the `distribute()` finding: a function that should only ever be fed values derived from a trusted, validated source (real on-disk state, established through `ScanPoolMetadata`/`LinkRepositoryToObjectPool`) instead blindly trusts caller-supplied fields and persists them into an accounting store used to drive pool lifecycle and deduplication decisions.

### Finding Description
`ScanPoolMetadata` is the trusted producer of pool-membership facts: it walks the storage, calls `stats.AlternatesInfoForRepository` on every repository, resolves the actual alternates path, and validates the pool directory with `storage.ValidateGitDirectory` before ever reporting a `(relative_path, pool_disk_path)` pair. [1](#0-0) 

`StorePoolMetadata`, however, is a separate RPC that receives a stream of the same-shaped tuples and persists them verbatim — it never re-derives or re-validates any of `storage_name`, `relative_path`, `pool_disk_path`, or `is_upstream` against disk state; it only checks that `storage_name` is non-empty: [2](#0-1) 

The proto documents the `InternalGitaly` service as "meant to be served by a Gitaly node, but only reachable by Praefect or other Gitalies," implying an assumed caller restriction: [3](#0-2) 

However, this restriction is only a comment/convention, not an enforced access-control boundary — the service is registered like every other Gitaly service, gated solely by the same shared-secret bearer-token check (`gitalyauth.CheckToken`) applied uniformly to all RPCs on all listeners: [4](#0-3) 

There is no per-service or per-caller authorization layer that restricts `StorePoolMetadata` to Praefect/Gitaly-to-Gitaly traffic specifically; any holder of the standard Gitaly auth token — the same credential used by ordinary Workhorse/Rails/Shell-driven git operations — can invoke it directly. Because the RPC persists whatever `(relative_path, pool_disk_path, is_upstream)` tuples it is given without cross-checking them against `ScanPoolMetadata`'s ground truth, a caller can register an arbitrary victim repository as a "member" (or "upstream") of an arbitrary pool disk path it does not actually reference on disk, exactly analogous to how `distribute()` let an attacker feed an arbitrary `principalAddress` that the Pool then trusted for accounting without validation.

### Impact Explanation
The pool-metadata store (`relational.PoolStore`) is the authoritative bookkeeping used by `ObjectPoolStateManager` and the `pool` CLI tooling to track which repositories are linked to which object pools, and to identify "broken pools" and drive cleanup/GC decisions: [5](#0-4) 

Corrupting this database with fabricated member/upstream records can:
- Cause GC/housekeeping tooling relying on this table to mis-attribute repositories to pools they are not actually linked to (or vice versa), producing incorrect deduplication/consistency operations and disrupting the accounting used by administrative and Praefect-scan flows.
- Falsely flag legitimate pools/members as "broken" or hide real broken links, undermining the housekeeping automation built on top of this store.
- Enable data poisoning across storages/pools that other components (CLI `pool` subcommand, `ListPoolMetadata`, `ListPoolUpstreams`) subsequently read as trustworthy, propagating false state through the system — a disruption of the pool's accounting parallel to the original finding's "Pool will mistake this token as its asset token."

This does not directly leak object bytes across repositories (the on-disk alternates file / `LinkRepositoryToObjectPool` path is separately validated), but it corrupts the metadata layer that is meant to mirror and drive decisions about real on-disk object-pool relationships, which is the "pool's accounting" analog to the report.

### Likelihood Explanation
Any client that has been issued the standard Gitaly authentication token can reach this RPC — there is no additional binding to "Praefect or Gitaly node" identity beyond the shared token comment in the proto. Since the token is the same one used for ordinary, non-administrative git operations across the system, the effective caller population is broader than the "internal-only" design intent, making exploitation straightforward for anyone with legitimate API access, without needing to compromise Praefect or another Gitaly node specifically.

### Recommendation
- Enforce that `StorePoolMetadata` (and other `InternalGitaly` RPCs) can only be invoked by genuinely internal callers (e.g., restrict to the internal Unix socket / require a distinct internal-only credential, not the general-purpose Gitaly auth token shared with all clients).
- Independently of caller identity, validate every `(relative_path, pool_disk_path, is_upstream)` tuple against real on-disk state before persisting — verify the pool path is a valid Git directory (`storage.ValidateGitDirectory`) and that the member repository's `objects/info/alternates` actually resolves to that pool, mirroring the checks already performed in `ScanPoolMetadata` and `LinkRepositoryToObjectPool`/`Disconnect`.
- Consider making `StorePoolMetadata` idempotently re-derive membership from disk rather than trusting the wire payload, or at minimum reject entries that fail validation instead of unconditionally writing them.

### Proof of Concept
1. Obtain the standard Gitaly bearer token (the same one used for regular gRPC operations, e.g. as GitLab Rails/Workhorse would use).
2. Open a `StorePoolMetadata` client-streaming call directly against the `InternalGitaly` service:
```
stream, _ := internalGitalyClient.StorePoolMetadata(ctx)
stream.Send(&gitalypb.StorePoolMetadataRequest{
    StorageName:  "default",
    RelativePath: "victim/private-repo.git",
    PoolDiskPath: "@pools/aa/bb/attacker-controlled-pool.git",
    IsUpstream:   true,
})
stream.CloseAndRecv()
``` [6](#0-5) 
3. No validation occurs — the handler only checks `storage_name` is non-empty and then persists the fabricated tuple via `poolStore.StorePoolData`, corrupting the accounting DB for a repository/pool pair that has no actual on-disk relationship: [7](#0-6) 
4. Subsequent reads via `ListPoolMetadata`/`ListPoolUpstreams` or CLI `pool` tooling will reflect the poisoned, false membership data as if it were derived from a real scan.

### Citations

**File:** internal/gitaly/service/internalgitaly/scan_pool_metadata.go (L45-87)
```go
	return func(relPath string, fi fs.FileInfo) error {
		repoPath := filepath.Join(storagePath, relPath)

		altInfo, err := stats.AlternatesInfoForRepository(repoPath)
		if err != nil {
			return fmt.Errorf("read alternates for %q: %w", relPath, err)
		}

		if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
			return nil
		}

		absPoolPaths := altInfo.AbsoluteObjectDirectories()
		if len(absPoolPaths) == 0 {
			return nil
		}

		poolObjectDir := absPoolPaths[0]
		poolRepoPath := filepath.Dir(poolObjectDir)

		poolDiskPath, err := filepath.Rel(storagePath, poolRepoPath)
		if err != nil {
			return fmt.Errorf("compute relative path for pool %q (repo %q): %w", poolRepoPath, relPath, err)
		}
		poolDiskPath = filepath.ToSlash(poolDiskPath)

		// We could encounter the same invalid pool multiple times.
		if invalidPools[poolDiskPath] {
			recordBrokenPool(ctx, poolStore, logger, storageName, relPath, poolDiskPath)
			return nil
		}

		if err := storage.ValidateGitDirectory(poolRepoPath); err != nil {
			invalidPools[poolDiskPath] = true
			recordBrokenPool(ctx, poolStore, logger, storageName, relPath, poolDiskPath)
			return nil
		}

		return stream.Send(&gitalypb.ScanPoolMetadataResponse{
			RelativePath: relPath,
			PoolDiskPath: poolDiskPath,
		})
	}
```

**File:** internal/gitaly/service/internalgitaly/store_pool_metadata.go (L13-63)
```go
func (s *server) StorePoolMetadata(stream gitalypb.InternalGitaly_StorePoolMetadataServer) error {
	if s.poolStore == nil {
		return structerr.NewFailedPrecondition("pool metadata store not configured")
	}

	poolsByDiskPath := make(map[string]*relational.PoolMetadata)
	var storageName string

	for {
		req, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return structerr.NewInternal("receive: %w", err)
		}

		if storageName == "" {
			storageName = req.GetStorageName()
			if storageName == "" {
				return structerr.NewInvalidArgument("storage_name is required")
			}
		}

		relPath := req.GetRelativePath()
		poolDiskPath := req.GetPoolDiskPath()
		isUpstream := req.GetIsUpstream()

		if _, exists := poolsByDiskPath[poolDiskPath]; !exists {
			poolsByDiskPath[poolDiskPath] = &relational.PoolMetadata{
				DiskPath:    poolDiskPath,
				StorageNode: storageName,
				Members:     []string{},
				UpdatedAt:   time.Now(),
			}
		}

		pool := poolsByDiskPath[poolDiskPath]
		pool.Members = append(pool.Members, relPath)
		if isUpstream {
			pool.Upstream = relPath
		}
	}

	if len(poolsByDiskPath) > 0 {
		if err := s.poolStore.StorePoolData(stream.Context(), storageName, poolsByDiskPath); err != nil {
			return structerr.NewInternal("store pool data: %w", err)
		}
	}

	return stream.SendAndClose(&gitalypb.StorePoolMetadataResponse{})
```

**File:** proto/internal.proto (L10-12)
```text
// InternalGitaly is a gRPC service meant to be served by a Gitaly node, but
// only reachable by Praefect or other Gitalies
service InternalGitaly {
```

**File:** internal/gitaly/server/auth/auth.go (L56-81)
```go
func checkFunc(conf gitalycfgauth.Config) func(ctx context.Context) (context.Context, error) {
	return func(ctx context.Context) (context.Context, error) {
		if len(conf.GetToken()) == 0 {
			countStatus("server disabled authentication", conf.Transitioning).Inc()
			return ctx, nil
		}

		err := gitalyauth.CheckToken(ctx, conf.GetToken(), time.Now())
		switch status.Code(err) {
		case codes.OK:
			countStatus(okLabel(conf.Transitioning), conf.Transitioning).Inc()
		case codes.Unauthenticated:
			countStatus("unauthenticated", conf.Transitioning).Inc()
		case codes.PermissionDenied:
			countStatus("denied", conf.Transitioning).Inc()
		default:
			countStatus("invalid", conf.Transitioning).Inc()
		}

		if conf.Transitioning {
			err = nil
		}

		return ctx, err
	}
}
```

**File:** internal/gitaly/storage/relational/state_manager.go (L10-20)
```go
// ObjectPoolStateManager updates the object pool metadata state
type ObjectPoolStateManager interface {
	// NotifyCreatePool records a new object pool in the database
	NotifyCreatePool(ctx context.Context, poolDiskPath, storageName, upstreamPath string) error
	// NotifyDeletePool removes an object pool and its members from the database
	NotifyDeletePool(ctx context.Context, poolDiskPath string) error
	// NotifyLinkRepository adds a repository as a member of an object pool
	NotifyLinkRepository(ctx context.Context, poolDiskPath, memberDiskPath string) error
	// NotifyUnlinkRepository removes a repository from an object pool's members
	NotifyUnlinkRepository(ctx context.Context, poolDiskPath, memberDiskPath string) error
}
```

**File:** internal/gitaly/storage/relational/sqlite.go (L61-122)
```go
// StorePoolData stores the given pool metadata in the database, replacing all
// existing data for the specified storage.
func (s *SQLitePoolStore) StorePoolData(ctx context.Context, storageName string, poolsByDiskPath map[string]*PoolMetadata) (returnErr error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin transaction: %w", err)
	}
	defer func() {
		if returnErr != nil {
			returnErr = errors.Join(returnErr, tx.Rollback())
		}
	}()

	if _, err := tx.ExecContext(ctx, `DELETE FROM pool_members WHERE pool_disk_path IN (SELECT disk_path FROM pools WHERE storage = ?)`, storageName); err != nil {
		return fmt.Errorf("delete pool members: %w", err)
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM pools WHERE storage = ?`, storageName); err != nil {
		return fmt.Errorf("delete pools: %w", err)
	}

	poolStmt, err := tx.PrepareContext(ctx, `
		INSERT INTO pools (disk_path, storage, last_scanned)
		VALUES (?, ?, ?)
	`)
	if err != nil {
		return fmt.Errorf("prepare pool statement: %w", err)
	}
	defer func() { returnErr = errors.Join(returnErr, poolStmt.Close()) }()

	memberStmt, err := tx.PrepareContext(ctx, `
		INSERT INTO pool_members (member_disk_path, pool_disk_path, is_upstream)
		VALUES (?, ?, ?)
	`)
	if err != nil {
		return fmt.Errorf("prepare member statement: %w", err)
	}
	defer func() { returnErr = errors.Join(returnErr, memberStmt.Close()) }()

	for diskPath, pool := range poolsByDiskPath {
		_, err := poolStmt.ExecContext(ctx, diskPath, storageName, pool.UpdatedAt)
		if err != nil {
			return fmt.Errorf("insert pool %s: %w", diskPath, err)
		}

		for _, memberDiskPath := range pool.Members {
			isUpstream := 0
			if pool.Upstream != "" && memberDiskPath == pool.Upstream {
				isUpstream = 1
			}
			_, err := memberStmt.ExecContext(ctx, memberDiskPath, diskPath, isUpstream)
			if err != nil {
				return fmt.Errorf("insert member %s for pool %s: %w", memberDiskPath, diskPath, err)
			}
		}
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit transaction: %w", err)
	}

	return nil
}
```
