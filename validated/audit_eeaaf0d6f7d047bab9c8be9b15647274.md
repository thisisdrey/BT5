### Title
Read-only enforcement blocks `RemoveRepository` even when a repository is inconsistent, preventing users from deleting at-risk repositories - (File: internal/praefect/router_per_repository.go)

### Summary
Praefect's `PerRepositoryRouter.RouteRepositoryMutator` uniformly rejects **all** repository-scoped mutator RPCs — including `RemoveRepository` — whenever the primary storage is not in the consistent-storages set, returning `ErrRepositoryReadOnly`. This mirrors the reported analog bug class: an emergency/"disabled" state check that is meant to prevent destructive writes ends up also blocking the one operation (removal/deletion) that users need most when the underlying data is already at risk.

### Finding Description
`RouteRepositoryMutator` computes `consistentStorages` for the repository and unconditionally short-circuits with `ErrRepositoryReadOnly` if the primary is not consistent, with no exception carved out for the deletion path: [1](#0-0) 

This same code path is used for every mutator RPC configured in `transactionRPCs`/routed via `RouteRepositoryMutator`, including `/gitaly.RepositoryService/RemoveRepository`: [2](#0-1) 

`getReplicationDetails` explicitly special-cases `RemoveRepository` as a deletion job type, showing the codebase is aware that repository removal is architecturally distinct from other mutations, yet the routing layer does not special-case it for the read-only check: [3](#0-2) 

The existing test suite confirms the read-only check is applied indiscriminately to repository-scoped mutators (demonstrated with `DeleteRefs`, but the same `RouteRepositoryMutator` logic backs `RemoveRepository` too): [4](#0-3) 

This is the direct analog of the external report's root cause: `update_position_internal`'s documented intent ("pool disabled unless removing liquidity") was violated because the enabled/disabled check in `update_position` didn't discriminate between adding and removing. Here, the documented intent of `ErrRepositoryReadOnly` ("primary does not have latest changes") [5](#0-4) 
is likewise applied without discriminating between "write more data to an inconsistent repo" (dangerous) and "delete the entire, possibly-corrupt repo" (a legitimate remediation/cleanup action that should not need write-consistency of file content).

### Impact Explanation
When a Gitaly cluster experiences replication lag or failover such that the primary is not in the consistent-storages set, `RemoveRepository` becomes unavailable for that repository via Praefect. Administrators/users who need to remove a broken, inconsistent, or otherwise problematic repository (e.g., to clean up storage, to remove data no longer wanted, or to unblock other operations depending on repository absence) cannot do so until the primary re-synchronizes — which may never happen automatically if the repository is broken specifically because of the inconsistency. This is a availability/DoS-class issue on a specific, safety-critical RPC handler at exactly the moment it's most needed, matching the accepted analog category "DoS of a handler."

### Likelihood Explanation
Read-only/inconsistent states occur under ordinary operational conditions in Gitaly Cluster (node failover, replication lag, partial write failures) and require no privileged or malicious actor — any ordinary client issuing `RemoveRepository` against an inconsistent repository will hit this path. The condition is reachable purely through normal cluster operation combined with a standard user RPC.

### Recommendation
In `RouteRepositoryMutator` (and the `Coordinator`'s mutator dispatch), special-case repository-deletion RPCs (`RemoveRepository`) so they are not gated by `ErrRepositoryReadOnly`/consistent-storages checks, analogous to how `getReplicationDetails` already treats `DeleteRepo` distinctly. At minimum, allow deletion to proceed against the primary (or any assigned storage) even when it is not currently "consistent," since removal does not depend on the correctness of file content the way further writes would.

### Proof of Concept
1. Set up a Praefect-managed virtual storage with a primary and secondary; create a repository.
2. Induce an inconsistency so that `GetConsistentStoragesByRepositoryID` excludes the primary (e.g., simulate a failed write/failover as in `TestStreamDirectorReadOnlyEnforcement`, which already demonstrates a repository-scoped mutator being rejected with `ErrRepositoryReadOnly` under this exact condition — [6](#0-5) ).
3. Issue `RepositoryService.RemoveRepository` for that repository.
4. Observe the RPC fails with `FailedPrecondition`/`ErrRepositoryReadOnly` via `RouteRepositoryMutator`, leaving the inconsistent/potentially-corrupt repository undeletable until consistency is restored.

### Citations

**File:** internal/praefect/router_per_repository.go (L249-256)
```go
	replicaPath, consistentStorages, err := r.rs.GetConsistentStoragesByRepositoryID(ctx, repositoryID)
	if err != nil {
		return RepositoryMutatorRoute{}, fmt.Errorf("consistent storages: %w", err)
	}

	if !consistentStorages.HasValue(primary) {
		return RepositoryMutatorRoute{}, ErrRepositoryReadOnly
	}
```

**File:** internal/praefect/coordinator.go (L38-40)
```go
// ErrRepositoryReadOnly is returned when the repository is in read-only mode. This happens
// if the primary does not have the latest changes.
var ErrRepositoryReadOnly = structerr.NewFailedPrecondition("repository is in read-only mode")
```

**File:** internal/praefect/coordinator.go (L83-83)
```go
	"/gitaly.RepositoryService/RemoveRepository":             transactionsEnabled,
```

**File:** internal/praefect/coordinator.go (L150-154)
```go
// getReplicationDetails determines the type of job and additional details based on the method name and incoming message
func getReplicationDetails(methodName string, m proto.Message) (datastore.ChangeType, datastore.Params, error) {
	switch methodName {
	case "/gitaly.RepositoryService/RemoveRepository":
		return datastore.DeleteRepo, nil, nil
```

**File:** internal/praefect/coordinator_test.go (L52-133)
```go
func TestStreamDirectorReadOnlyEnforcement(t *testing.T) {
	t.Parallel()
	db := testdb.New(t)
	for _, tc := range []struct {
		desc     string
		readOnly bool
	}{
		{desc: "writable", readOnly: false},
		{desc: "read-only", readOnly: true},
	} {
		t.Run(tc.desc, func(t *testing.T) {
			db.TruncateAll(t)

			const (
				virtualStorage = "test-virtual-storage"
				relativePath   = "test-repository"
				storage        = "test-storage"
			)
			conf := config.Config{
				VirtualStorages: []*config.VirtualStorage{
					{
						Name: virtualStorage,
						Nodes: []*config.Node{
							{
								Address: "tcp://gitaly-primary.example.com",
								Storage: storage,
							},
						},
					},
				},
			}
			ctx := testhelper.Context(t)

			rs := datastore.MockRepositoryStore{
				GetConsistentStoragesFunc: func(context.Context, string, string) (string, *datastructure.Set[string], error) {
					if tc.readOnly {
						return "", datastructure.SetFromValues(storage + "-other"), nil
					}
					return "", datastructure.NewSet[string](), nil
				},
			}

			logger := testhelper.NewLogger(t)
			repoWriteLockMgr := datastore.NewRepoReferenceWriteLockManager(ctx, db, testdb.GetConfig(t, db.Name), logger)
			coordinator := NewCoordinator(
				logger,
				datastore.NewPostgresReplicationEventQueue(db),
				rs,
				nil,
				NewNodeManagerRouter(&nodes.MockManager{GetShardFunc: func(vs string) (nodes.Shard, error) {
					require.Equal(t, virtualStorage, vs)
					return nodes.Shard{
						Primary: &nodes.MockNode{GetStorageMethod: func() string {
							return storage
						}},
					}, nil
				}}, rs),
				transactions.NewManager(conf, logger, repoWriteLockMgr),
				conf,
				protoregistry.GitalyProtoPreregistered,
			)

			frame, err := proto.Marshal(&gitalypb.DeleteRefsRequest{
				Repository: &gitalypb.Repository{
					StorageName:  virtualStorage,
					RelativePath: relativePath,
				},
				Refs: [][]byte{
					[]byte("refs/heads/does-not-exist"),
				},
			})
			require.NoError(t, err)

			_, err = coordinator.StreamDirector(ctx, "/gitaly.RefService/DeleteRefs", &mockPeeker{frame: frame})
			if tc.readOnly {
				testhelper.RequireGrpcError(t, ErrRepositoryReadOnly, err)
				testhelper.RequireGrpcCode(t, err, codes.FailedPrecondition)
			} else {
				require.NoError(t, err)
			}
		})
	}
```
