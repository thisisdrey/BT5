### Title
Predictable, deterministic replica-path derivation allows pre-creation squatting that permanently blocks legitimate repository/fork/object-pool creation - (File: `internal/gitaly/storage/repository_path.go`, `internal/gitaly/repoutil/create.go`)

### Summary
Gitaly (behind Praefect) derives every repository's on-disk storage path from a globally-shared, strictly monotonically increasing `repository_id` sequence using a public, unsalted hash function. Any actor who can predict a soon-to-be-assigned `repository_id` can pre-create Git-directory content at that exact future path. Because repository creation (`CreateRepository`, `CreateFork`, `CreateObjectPool`, `CreateRepositoryFromBundle`, etc.) unconditionally aborts with `AlreadyExists`/`FailedPrecondition` if *anything* already occupies the target path, the legitimate owner of that repository ID can never have their repository created — a permanent, targeted denial of service. This mirrors the reported BakerFi bug class: an attacker seeds a not-yet-initialized shared resource (the Strategy / here, the future repository path) before the legitimate initializer arrives, permanently corrupting/blocking normal use of that resource.

### Finding Description
`storage.DeriveReplicaPath` computes a repository's disk path purely as a function of its numeric `repository_id`: [1](#0-0) 

The hash input is only `strconv.FormatInt(repositoryID, 10)` — no secret, salt, or randomness is involved, so the path for any future ID is fully predictable given the ID. The `repository_id` itself comes from a plain Postgres sequence, `START WITH 1 INCREMENT BY 1`, shared across all repositories on a virtual storage: [2](#0-1) 

and is reserved via `ReserveRepositoryID` in `RouteRepositoryCreation`: [3](#0-2) 

`ReserveRepositoryID` simply increments the sequence and returns the next value; the tests explicitly confirm the sequential, deterministic nature (`1`, `2`, `3`, ...): [4](#0-3) 

Because any repository creation (including one initiated by the attacker themselves, e.g. creating their own project/fork) consumes one ID from this shared sequence, an attacker can trivially learn the current counter value and thus predict the exact `RelativePath` (`@cluster/repositories/xx/yy/<id>`) that will be assigned to a subsequent, unrelated repository created by another user.

On the actual creation path, `repoutil.Create` treats *any* existing filesystem entry at the target path as fatal and irrevocably fails the creation: [5](#0-4) 

with an identical re-check after acquiring the lock: [6](#0-5) 

The project's own tests demonstrate and codify this exact conflict scenario for `CreateFork`, deliberately using `storage.DeriveReplicaPath(2)` to pre-seed the *next* replica path Praefect will assign and confirming that doing so makes the legitimate creation fail with `AlreadyExists` (or, under WAL, `invalid git directory`): [7](#0-6) 

The same "already exists"/`FailedPrecondition` behavior exists for object pools: [8](#0-7) 

and `CreateRepository` itself surfaces the identical error for a pre-existing target: [9](#0-8) 

There is no mechanism analogous to "seeding the vault" (the BakerFi team's fix) here: no reservation of the on-disk path happens atomically with the sequence-number reservation, and the ID→path derivation contains no unpredictable component, so any writer able to place a directory (or even an empty file) at a predicted future replica path can permanently deny that specific repository ID from ever being materialized on disk.

### Impact Explanation
Once a target replica path is squatted, the specific repository (fork, object pool, or bundle-imported project) that would have been assigned that `repository_id` can **never** be created — `repoutil.Create`'s pre-lock and post-lock existence checks fail deterministically every time a retry is attempted, since the squatted directory remains in place. This is a persistent, repeatable denial of service against a specific, attacker-chosen victim operation (e.g., a targeted user's fork creation, or a specific object pool linkage step in the fork flow), not just a generic resource-exhaustion DoS. Depending on deployment (WAL vs. legacy backend), the failure surfaces as `AlreadyExists` or as a corrupted-partition validation error, but in both cases the operation is unrecoverable without manual operator intervention to clear the squatted path.

### Likelihood Explanation
The `repository_id` sequence is shared and globally incrementing, and any user capable of triggering repository creation (creating a project, forking, importing) consumes an ID and can thus observe/estimate the current counter value with a small window of uncertainty (accounting for concurrent activity by other users). `DeriveReplicaPath`'s formula is fully public (it is documented and tested in the codebase itself). The remaining requirement is the ability to write a directory/file at an arbitrary, not-yet-existing `RelativePath` on a storage — achievable via any Gitaly repository-creation RPC that accepts a client-supplied `RelativePath` (`CreateRepository`, `CreateRepositoryFromBundle`, `CreateRepositoryFromSnapshot`, `CreateObjectPool`) when such an RPC is reachable, as these RPCs pass the caller-supplied path straight through to `repoutil.Create`/`locator.GetRepoPath` without any secondary authorization tied to the intended owner: [10](#0-9) 

### Recommendation
- Bind path reservation to `repository_id` reservation atomically (e.g., have `ReserveRepositoryID` also create a placeholder/lock record for the derived replica path in the same transaction), so a pre-existing filesystem entry at a not-yet-reserved path is detected and remediated (or the ID re-rolled) instead of causing a hard, permanent failure.
- Introduce an unpredictable component (e.g., a per-repository random salt persisted alongside `repository_id`) into `DeriveReplicaPath`/`DerivePoolPath` so future paths cannot be computed by external actors from the sequence value alone.
- Have `repoutil.Create` distinguish between "path occupied by unrelated/foreign content" (potential squat) versus genuine concurrent creation, and support automatic cleanup/quarantine-and-retry of orphaned, non-owned directories found at a freshly reserved repository path.

### Proof of Concept
1. Attacker calls any project/fork/import-creation flow to learn (or closely estimate) the current value `N` of the shared `repositories_repository_id_seq` (e.g., their own newly created repository is assigned ID `N`, as shown by `TestPerRepositoryRouterRouteRepositoryCreation`/`ReserveRepositoryID` returning sequential IDs — [4](#0-3) ).
2. Attacker computes `path = storage.DeriveReplicaPath(N+1)` (or `N+2`, etc.) using the public, documented formula — [11](#0-10) .
3. Attacker issues a `CreateRepository`/`CreateRepositoryFromBundle` (or any RPC accepting an explicit `RelativePath`) request with `RelativePath = path`, causing a directory to be created at that on-disk location ahead of the legitimate future repository.
4. When another user's action (e.g., `CreateFork`) is later routed by Praefect and reserves ID `N+1`, deriving the identical `path`, the resulting `repoutil.Create` call fails permanently with `structerr.NewAlreadyExists("repository exists already")` (or the WAL equivalent "invalid git directory") — exactly as reproduced by the repository's own test: [12](#0-11) 
The targeted user's fork/repository can never be created at that ID; the legitimate creation flow has no self-healing path around the squatted directory.

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

**File:** _support/praefect-schema.sql (L286-301)
```sql
-- Name: repositories_repository_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.repositories_repository_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: repositories_repository_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.repositories_repository_id_seq OWNED BY public.repositories.repository_id;
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

**File:** internal/praefect/datastore/repository_store_test.go (L1036-1056)
```go
	t.Run("ReserveRepositoryID", func(t *testing.T) {
		rs := newRepositoryStore(t, nil)

		id, err := rs.ReserveRepositoryID(ctx, vs, repo)
		require.NoError(t, err)
		require.Equal(t, int64(1), id)

		id, err = rs.ReserveRepositoryID(ctx, vs, repo)
		require.NoError(t, err)
		require.Equal(t, int64(2), id)

		require.NoError(t, rs.CreateRepository(ctx, id, vs, repo, "replica-path", stor, nil, nil, false, false))

		id, err = rs.ReserveRepositoryID(ctx, vs, repo)
		require.Equal(t, ErrRepositoryAlreadyExists, err)
		require.Equal(t, int64(0), id)

		id, err = rs.ReserveRepositoryID(ctx, vs, repo+"-2")
		require.NoError(t, err)
		require.Equal(t, int64(3), id)
	})
```

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

**File:** internal/gitaly/service/repository/create_fork_test.go (L303-382)
```go
func TestCreateFork_targetExists(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		desc        string
		seed        func(t *testing.T, targetPath string)
		expectedErr error
	}{
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
		{
			desc: "non-empty target directory",
			seed: func(t *testing.T, targetPath string) {
				require.NoError(t, os.MkdirAll(targetPath, mode.Directory))
				require.NoError(t, os.WriteFile(
					filepath.Join(targetPath, "config"),
					nil,
					mode.File,
				))
			},
			expectedErr: func() error {
				if testhelper.IsWALEnabled() {
					return structerr.NewInternal("begin transaction: get partition: get partition ID: validate git directory: invalid git directory")
				}

				return structerr.NewAlreadyExists("creating fork: repository exists already")
			}(),
		},
		{
			desc: "target file",
			seed: func(t *testing.T, targetPath string) {
				require.NoError(t, os.MkdirAll(filepath.Dir(targetPath), mode.Directory))
				require.NoError(t, os.WriteFile(targetPath, nil, mode.File))
			},
			expectedErr: func() error {
				if testhelper.IsWALEnabled() {
					return structerr.NewInternal("begin transaction: get partition: get partition ID: validate git directory: not a directory")
				}

				return structerr.NewAlreadyExists("creating fork: repository exists already")
			}(),
		},
	} {
		t.Run(tc.desc, func(t *testing.T) {
			ctx := testhelper.Context(t)
			cfg, client := setupRepositoryService(t)
			ctx = testhelper.MergeOutgoingMetadata(ctx, testcfg.GitalyServersMetadataFromCfg(t, cfg))

			repo, _ := gittest.CreateRepository(t, ctx, cfg)

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
	}
}
```

**File:** internal/git/objectpool/create.go (L42-48)
```go
	if _, err := os.Stat(objectPoolPath); err == nil {
		return nil, structerr.NewFailedPrecondition("target path exists already").
			WithMetadata("object_pool_path", objectPoolPath)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, structerr.NewInternal("checking object pool existence: %w", err).
			WithMetadata("object_pool_path", objectPoolPath)
	}
```

**File:** internal/gitaly/service/repository/create_repository_test.go (L304-311)
```go
		{
			desc: "preexisting repository",
			repo: preexistingRepo,
			expectedErr: structerr.NewAlreadyExists("%s", testhelper.GitalyOrPraefect(
				"creating repository: repository exists already",
				"route repository creation: reserve repository id: repository already exists",
			)),
		},
```

**File:** internal/gitaly/service/repository/create_repository.go (L15-19)
```go
func (s *server) CreateRepository(ctx context.Context, req *gitalypb.CreateRepositoryRequest) (*gitalypb.CreateRepositoryResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```
