### Title
Attacker can pre-create a repository/object-pool target path to permanently block legitimate repository creation - (File: `internal/gitaly/repoutil/create.go`)

### Summary
Gitaly's repository-creation helper `repoutil.Create` refuses to proceed if the target repository path already exists on disk, returning a hard `AlreadyExists` error with no fallback (analogous to Anchor's `init` constraint failing when an ATA already exists). Because several public RPCs (`CreateRepository`, `CreateFork`, `CreateRepositoryFromBundle`, `CreateRepositoryFromURL`, `CreateObjectPool`) let the caller supply an arbitrary `RelativePath` for the *target* repository, any client entitled to invoke these mutator RPCs can pre-create a directory/repository at a path that another legitimate actor will later need, permanently denying that specific repository/fork/object-pool creation.

### Finding Description
`repoutil.Create` performs a pre-lock existence check and, after acquiring the repository lock, a second post-lock existence check; if the target path already exists it returns `structerr.NewAlreadyExists("repository exists already")` and never overwrites or reuses the location: [1](#0-0) [2](#0-1) 

The same "exists already" hard-fail pattern is used for object pools: [3](#0-2) 

All of `CreateRepository`, `CreateFork`, `CreateRepositoryFromBundle`, `CreateRepositoryFromURL`, and `CreateObjectPool` funnel through `repoutil.Create`/`objectpool.Create` and accept a caller-controlled target `Repository.RelativePath`: [4](#0-3) [5](#0-4) [6](#0-5) 

When Praefect fronts Gitaly, the target's actual on-disk `replica_path` is derived deterministically from a monotonically-increasing, globally shared `repository_id` sequence: [7](#0-6) [8](#0-7) 

Because the sequence is shared and monotonic, an attacker who can trigger repository creations (consuming sequence values) can predict the `repository_id`/replica path that will be assigned to a subsequent, unrelated creation (e.g. a victim's fork or a project's object pool) and pre-create/lock that exact path beforehand, or — in Gitaly-only deployments without Praefect, where the `RelativePath` is supplied directly by the caller/Rails using a predictable hashed-storage scheme — directly target that path. Tests demonstrate the resulting hard failure once the target path is pre-seeded, with the comment explicitly noting the path is being pre-computed and pre-created for exactly this collision: [9](#0-8) [10](#0-9) 

This mirrors the reported bug class exactly: a resource whose address is derivable in advance is created ahead of time by an attacker, so that Anchor's (here, Gitaly's) `init`-style "must not exist" check unconditionally fails the legitimate actor's later, otherwise-valid operation, with no automatic recovery path (`init_if_needed`/idempotent handling equivalent does not exist here).

### Impact Explanation
A successful pre-creation permanently prevents the specific repository/fork/object pool from ever being created at that path via the normal RPCs, since `repoutil.Create`/`objectpool.Create` never overwrite or reclaim an existing target. This is a persistent, targeted denial of service against a specific project/fork/object-pool creation attempt (`CreateRepository`, `CreateFork`, `CreateRepositoryFromBundle`, `CreateRepositoryFromURL`, `CreateObjectPool`) — matching the reported bug's "Medium" impact classification: no data is stolen or corrupted, but a specific target becomes permanently uncreatable without manual/administrative intervention (removing the pre-created directory and/or database row out-of-band).

### Likelihood Explanation
Any client with authorization to call the affected mutator RPCs (`CreateRepository`, `CreateRepositoryFromBundle`, `CreateRepositoryFromURL`, `CreateFork`, `CreateObjectPool`) can attempt this without special privileges, matching the "ordinary user...fork, import, or crafted RPC field" reachability criterion. The main practical constraint is predicting the exact `RelativePath`/`replica_path` in advance (feasible given the monotonic, globally-shared ID sequence in Praefect's `ReserveRepositoryID`, or a fully caller-supplied path in non-Praefect deployments), which raises the bar versus the original Solana report's fully deterministic PDA derivation but does not eliminate it.

### Recommendation
- For target-repository RPCs where the caller supplies `RelativePath` (`CreateRepository`, `CreateRepositoryFromBundle`, `CreateRepositoryFromURL`, `CreateFork`, `CreateObjectPool`), avoid making irreversible "already exists → permanent failure" the only outcome for repository IDs/paths that are not yet actually associated with any real, committed project/fork in the calling application (Rails/GitLab) — e.g., detect and clean up orphaned pre-created empty directories that don't correspond to a tracked project before failing permanently, or fence directory pre-creation behind the same authorization/ownership check that gates the eventual legitimate creation.
- Consider making `repository_id`/`replica_path` allocation less predictable/guessable by external actors (e.g., avoid strictly sequential, globally observable identifiers being convertible directly into filesystem paths reachable by arbitrary Gitaly RPC callers), or require that path pre-creation be strictly scoped/reserved to the same principal/session performing the real creation.

### Proof of Concept
1. Deploy Gitaly with Praefect, with an existing repository already created, so that the `repositories_repository_id_seq` value is known/observable (e.g., attacker owns a project of their own and knows their own assigned ID N).
2. Attacker predicts that the next repository/fork/object-pool creation from a victim (e.g. a fork of a public repository, or an object-pool for that repository) will receive ID `N+1`, and computes `storage.DeriveReplicaPath(N+1)` using the same public derivation function used in Gitaly's tests: [11](#0-10) 
3. Attacker calls `CreateRepositoryFromBundle`/`CreateRepositoryFromURL`/`CreateRepository` directly against a Gitaly node they can reach, specifying `RelativePath = DeriveReplicaPath(N+1)`, causing a directory (and/or a DB row via `ReserveRepositoryID`) to be created at that exact path.
4. When the victim's legitimate `CreateFork`/`CreateObjectPool` call is later routed and reserves ID `N+1` (or is routed directly to that exact replica path), `repoutil.Create`/`objectpool.Create`'s pre-existing-path check fails and the operation permanently errors with `AlreadyExists`/`FailedPrecondition`, exactly as exercised in: [12](#0-11) [13](#0-12)

### Citations

**File:** internal/gitaly/repoutil/create.go (L96-104)
```go
	// The repository must not exist on disk already, or otherwise we won't be able to
	// create it with atomic semantics.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("pre-lock stat: %w", err)
	}
```

**File:** internal/gitaly/repoutil/create.go (L197-208)
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
```

**File:** internal/git/objectpool/create.go (L37-48)
```go
	objectPoolPath, err := locator.GetRepoPath(ctx, proto.GetRepository(), storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	if _, err := os.Stat(objectPoolPath); err == nil {
		return nil, structerr.NewFailedPrecondition("target path exists already").
			WithMetadata("object_pool_path", objectPoolPath)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, structerr.NewInternal("checking object pool existence: %w", err).
			WithMetadata("object_pool_path", objectPoolPath)
	}
```

**File:** internal/gitaly/service/repository/create_repository.go (L15-19)
```go
func (s *server) CreateRepository(ctx context.Context, req *gitalypb.CreateRepositoryRequest) (*gitalypb.CreateRepositoryResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```

**File:** internal/gitaly/service/repository/create_fork.go (L16-32)
```go
func (s *server) CreateFork(ctx context.Context, req *gitalypb.CreateForkRequest) (*gitalypb.CreateForkResponse, error) {
	// We don't validate existence of the source repository given that we may connect to a different Gitaly host in
	// order to fetch from it. So it may or may not exist locally.
	if err := s.locator.ValidateRepository(ctx, req.GetSourceRepository(), storage.WithSkipStorageExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("validating source repository: %w", err)
	}

	// Neither do we validate existence of the target repository given that this is the repository we wish to create
	// in the first place.
	if err := s.locator.ValidateRepository(ctx, req.GetRepository(), storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	targetRepository := req.GetRepository()
	sourceRepository := req.GetSourceRepository()

	if err := repoutil.Create(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, targetRepository, func(repoProto *gitalypb.Repository) error {
```

**File:** internal/gitaly/service/objectpool/create.go (L17-29)
```go
func (s *server) CreateObjectPool(ctx context.Context, in *gitalypb.CreateObjectPoolRequest) (*gitalypb.CreateObjectPoolResponse, error) {
	if in.GetOrigin() == nil {
		return nil, errMissingOriginRepository
	}

	poolRepo := in.GetObjectPool().GetRepository()
	if poolRepo == nil {
		return nil, errMissingPool
	}

	if !storage.IsPoolRepository(poolRepo) {
		return nil, errInvalidPoolDir
	}
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

**File:** internal/praefect/datastore/repository_store.go (L884-904)
```go
// ReserveRepositoryID reserves an ID for a repository that is about to be created and returns it. If a repository already
// exists with the given virtual storage and relative path combination, an error is returned.
func (rs *PostgresRepositoryStore) ReserveRepositoryID(ctx context.Context, virtualStorage, relativePath string) (int64, error) {
	var id int64
	if err := rs.db.QueryRowContext(ctx, `
SELECT nextval('repositories_repository_id_seq')
WHERE NOT EXISTS (
	SELECT FROM repositories
	WHERE virtual_storage = $1
	AND   relative_path   = $2
)
	`, virtualStorage, relativePath).Scan(&id); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return 0, ErrRepositoryAlreadyExists
		}

		return 0, fmt.Errorf("scan: %w", err)
	}

	return id, nil
}
```

**File:** internal/gitaly/service/repository/create_fork_test.go (L311-323)
```go
		{
			desc: "empty target directory",
			seed: func(t *testing.T, targetPath string) {
				require.NoError(t, os.MkdirAll(targetPath, mode.Directory))
			},
			expectedErr: func() error {
				if testhelper.IsWALEnabled() {
					return structerr.NewInternal("begin transaction: get partition: get partition ID: validate git directory: invalid git directory")
				}

				return structerr.NewAlreadyExists("creating fork: repository exists already")
			}(),
		},
```

**File:** internal/gitaly/service/repository/create_fork_test.go (L364-380)
```go
			forkedRepo := &gitalypb.Repository{
				// As this test can run with Praefect in front of it, we'll use the next replica path Praefect will
				// assign in order to ensure this repository creation conflicts even with Praefect in front of it.
				// As the source repository created in the setup is the first one, this would get the repository
				// ID 2.
				RelativePath: storage.DeriveReplicaPath(2),
				StorageName:  repo.GetStorageName(),
			}

			tc.seed(t, filepath.Join(cfg.Storages[0].Path, forkedRepo.GetRelativePath()))

			_, err := client.CreateFork(ctx, &gitalypb.CreateForkRequest{
				Repository:       forkedRepo,
				SourceRepository: repo,
			})
			testhelper.RequireGrpcError(t, tc.expectedErr, err)
		})
```

**File:** internal/gitaly/service/objectpool/create_test.go (L49-53)
```go
	_, err := client.CreateObjectPool(ctx, &gitalypb.CreateObjectPoolRequest{
		ObjectPool: poolProto,
		Origin:     repo,
	})
	require.NoError(t, err)
```

**File:** internal/gitaly/service/objectpool/create_test.go (L250-257)
```go
		{
			desc: "pool exists",
			request: &gitalypb.CreateObjectPoolRequest{
				Origin:     repo,
				ObjectPool: preexistingPool,
			},
			expectedErr: structerr.NewAlreadyExists("creating object pool: repository exists already"),
		},
```
