### Title
Missing verification in `InternalGitaly.StorePoolMetadata` allows forged repository-to-pool relationships without actually scanning alternates - ([File: internal/gitaly/service/internalgitaly/store_pool_metadata.go])

### Summary
`InternalGitaly.StorePoolMetadata` is designed to be called only as the second step of a scan/store pipeline: `ScanPoolMetadata` walks a storage's repositories and reads their real `objects/info/alternates` files to discover genuine repo-to-pool relationships, and `StorePoolMetadata` is supposed to persist exactly that verified data. However, the `StorePoolMetadata` handler performs no verification at all that the `relative_path`/`pool_disk_path`/`is_upstream` triples it receives correspond to any actual on-disk alternates linkage. This mirrors the Vader `Pools.sync()` issue: a function meant to be called only after an upstream step performs the real checks (and, in Vader's case, the real token transfer; here, the real alternates scan) is exposed without re-validating that the precondition was met, so the caller can update authoritative accounting state (the pool metadata database) without the underlying fact it represents ever having occurred.

### Finding Description
The intended flow is `ScanPoolMetadata` (reads `objects/info/alternates` on disk) → `StorePoolMetadata` (persists what was found): [1](#0-0) 

The `StorePoolMetadata` server handler simply receives a stream of `(storage_name, relative_path, pool_disk_path, is_upstream)` tuples and blindly assembles/persists them, with the only checks being that `storage_name` is non-empty and the pool store is configured: [2](#0-1) 

There is no check that:
- `relative_path` actually exists as a repository on the named storage,
- `pool_disk_path` actually exists as an object pool on disk,
- the repository at `relative_path` actually has an `objects/info/alternates` entry pointing at `pool_disk_path` (the fact that `ScanPoolMetadata` is supposed to establish before this RPC is ever invoked),
- the `is_upstream` flag reflects the real upstream repository of the pool.

`SQLitePoolStore.StorePoolData` then performs a destructive `DELETE ... WHERE storage = ?` for both `pools` and `pool_members` tables scoped to the given storage, followed by inserting whatever was supplied in the request — i.e., a single call can wholesale replace the entire pool-membership bookkeeping for a storage with attacker-controlled data: [3](#0-2) 

This is directly analogous to `Pools.sync()`: the RPC that is meant to only be invoked after a prior, trusted verification step (the disk-based alternates scan) accepts and commits the same accounting update without re-checking that the verification actually happened.

### Impact Explanation
The pool metadata store (`pools`/`pool_members` tables) is the source of truth other tooling relies on for object-pool membership decisions — e.g., listing pool members, resolving `GetPoolForMember`, and administrative pool tooling (`gitaly pool` / `praefect pool` subcommands) that consume `ListPoolMetadata`/`ListPoolUpstreams`: [4](#0-3) 

Poisoning this table lets a caller claim a repository is (or is not) a member/upstream of an arbitrary object pool without that relationship existing on disk. Depending on how downstream consumers of this metadata make housekeeping/GC or cross-repository object-sharing decisions, this can corrupt object-pool bookkeeping cluster-wide for a storage (wholesale delete+replace per storage), enabling incorrect pool/member associations to be reported to administrative and cluster-management workflows, or a denial of service against pool tracking for a storage.

### Likelihood Explanation
`InternalGitaly` is documented as reachable only by Praefect or other Gitaly nodes over the internal gRPC socket: [5](#0-4) 

but the internal server is created with the same auth-token interceptor chain as the external server (no additional authorization tying the caller's identity to having actually performed a scan): [6](#0-5) [7](#0-6) 

This lowers the practical likelihood compared to a directly internet/user-reachable RPC — exploitation requires the ability to reach the internal socket/auth token (e.g., a compromised or malicious Praefect/Gitaly peer, or lateral access within the cluster), not an ordinary anonymous push/fetch. Given the "reject privileged-actor/malicious-peer" constraint in the validation rules, this weakens confidence that this fully qualifies as an "ordinary user" reachable analog; I flag this uncertainty explicitly since I could not find any external-user-facing RPC in the indexed portion of the codebase with the same "accept-without-reverifying-precondition" pattern that is reachable purely from push/fetch/fork/import.

### Recommendation
- Have `StorePoolMetadata` (or the pool store layer) re-verify each reported relationship against disk before persisting: confirm the repository exists at `relative_path`, the pool exists at `pool_disk_path`, and the repository's `objects/info/alternates` file actually references the pool's object directory.
- Alternatively, remove the two-RPC split and have a single internal-only operation perform the scan and store atomically, so store never accepts caller-supplied relationship data it hasn't independently derived.
- Add stronger authorization scoping for `InternalGitaly` RPCs (e.g., a dedicated internal-only credential/mTLS identity distinct from the general auth token) so it cannot be invoked by parties that shouldn't have pool-bookkeeping write access.

### Proof of Concept
1. Obtain access to a Gitaly node's internal gRPC socket/auth token (as would any Praefect node or co-located Gitaly in the cluster).
2. Call `InternalGitaly.StorePoolMetadata` directly with a crafted stream, e.g.:
```
StorePoolMetadataRequest{StorageName: "default", RelativePath: "victim-repo.git", PoolDiskPath: "@pools/aa/bb/attacker-pool.git", IsUpstream: true}
```
without ever having called `ScanPoolMetadata` or having `victim-repo.git` actually configured with an alternate pointing at `attacker-pool.git`.
3. `StorePoolData` deletes all existing pool/member rows for storage `default` and inserts the forged relationship as-is: [8](#0-7)  — the metadata database now reflects a pool relationship that does not exist on disk, with no cross-check ever performed by the handler: [9](#0-8) .

### Citations

**File:** proto/internal.proto (L22-40)
```text
  // ScanPoolMetadata scans a storage for repository-to-pool relationships
  // by reading alternates files. It streams back repositories that are
  // linked to object pools.
  rpc ScanPoolMetadata (ScanPoolMetadataRequest) returns (stream ScanPoolMetadataResponse) {
    option (op_type) = {
      op:          ACCESSOR
      scope_level: STORAGE
    };
  }

  // StorePoolMetadata stores repository-to-pool relationships into the
  // local pool metadata database. It receives a stream of (repo, pool) pairs
  // and merges them into the existing data.
  rpc StorePoolMetadata (stream StorePoolMetadataRequest) returns (StorePoolMetadataResponse) {
    option (op_type) = {
      op:          MUTATOR
      scope_level: STORAGE
    };
  }
```

**File:** internal/gitaly/service/internalgitaly/store_pool_metadata.go (L13-55)
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
```

**File:** internal/gitaly/storage/relational/sqlite.go (L61-119)
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
```

**File:** internal/gitaly/storage/relational/pool_store.go (L24-43)
```go
// PoolStore provides storage for object pool metadata.
type PoolStore interface {
	StorePoolData(ctx context.Context, storageName string, poolsByDiskPath map[string]*PoolMetadata) error
	GetPoolByDiskPath(ctx context.Context, poolDiskPath string) (*PoolMetadata, error)
	ListPools(ctx context.Context) ([]*PoolMetadata, error)
	ForEachPoolByStorage(ctx context.Context, storageName string, fn func(*PoolMetadata) error) error

	ListPoolMembers(ctx context.Context, poolDiskPath string) ([]string, error)
	DeletePoolMembers(ctx context.Context, poolDiskPath string) error
	GetPoolForMember(ctx context.Context, memberDiskPath string) (string, error)

	CreatePool(ctx context.Context, poolDiskPath, storageName, upstream string, lastScanned time.Time) error
	DeletePool(ctx context.Context, poolDiskPath string) error
	AddMember(ctx context.Context, poolDiskPath, memberDiskPath string) error
	RemoveMember(ctx context.Context, poolDiskPath, memberDiskPath string) error

	RecordBrokenPool(ctx context.Context, storage, poolMember, pool string) error

	Close() error
}
```

**File:** proto/go/gitalypb/internal_grpc.pb.go (L146-152)
```go
// InternalGitalyServer is the server API for InternalGitaly service.
// All implementations must embed UnimplementedInternalGitalyServer
// for forward compatibility.
//
// InternalGitaly is a gRPC service meant to be served by a Gitaly node, but
// only reachable by Praefect or other Gitalies
type InternalGitalyServer interface {
```

**File:** internal/gitaly/server/server.go (L163-166)
```go
		statushandler.AbortedErrorUnaryInterceptor,
		statushandler.Unary, // Should be below LogHandler and above AbortedInterceptor in case this returns Aborted in the future
		auth.UnaryServerInterceptor(s.cfg.Auth),
	}
```

**File:** internal/gitaly/server/server_factory.go (L109-119)
```go
// CreateInternal creates a new internal gRPC server. Internal servers are closed
// after the external ones when gracefully shutting down.
func (s *GitalyServerFactory) CreateInternal(opts ...Option) (*grpc.Server, error) {
	server, err := s.New(false, false, opts...)
	if err != nil {
		return nil, err
	}

	s.internalServers = append(s.internalServers, server)
	return server, nil
}
```
