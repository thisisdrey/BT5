### Title
Predictable Repository-ID-derived Disk Paths Allow Unprivileged Path Squatting That Blocks CreateFork/CreateObjectPool/CreateRepository - (File: internal/praefect/router_per_repository.go)

### Summary
Praefect's `PerRepositoryRouter.RouteRepositoryCreation` computes the on-disk replica path for every newly created repository or object pool deterministically from a monotonically-increasing, unauthenticated-to-predict `repository_id` sequence, using `storage.DeriveReplicaPath`/`storage.DerivePoolPath` (a SHA256 hash of the small decimal integer ID). Because Gitaly's `CreateRepository` RPC lets any caller specify an arbitrary `RelativePath` that is only checked for path-escape and pre-existence (not for whether it happens to be a predicted future path), an ordinary client can pre-create a directory at the path that will be assigned to the *next* repository ID before the legitimate `CreateFork`/`CreateObjectPool`/`CreateRepository` call claims it — causing the legitimate operation to fail with "repository already exists" and forcing repeated retries/ID burn, analogous to the reported `PublicLock.initialize()` front-running issue where an unprivileged actor races a predictable initialization target to force costly redeployment.

### Finding Description
`RouteRepositoryCreation` reserves a repository ID via `r.rs.ReserveRepositoryID` and then derives the physical replica path purely as a function of that integer ID: [1](#0-0) 

The derivation itself is a simple, unsalted SHA256 hash of the decimal string of the ID, producing a fully predictable path such as `@cluster/repositories/6b/86/1` for ID 1: [2](#0-1) [3](#0-2) 

The repository ID itself comes from a plain incrementing Postgres sequence/BIGSERIAL, so any client that can observe (or infer from repeated successful creations) the current highest allocated ID can compute the path that will be assigned to the *next* repository: [4](#0-3) 

Gitaly's `CreateRepository` handler accepts a client-supplied `RelativePath` and only validates that it doesn't escape the storage root and doesn't already exist — it performs no check that the path collides with a reserved, ID-derived path that a legitimate `CreateFork`/`CreateObjectPool` will soon try to claim: [5](#0-4) 

The underlying `repoutil.Create` only guards against a *second* creation of the *same exact path* via a pre-lock stat, a repository lock, and a post-lock re-stat — it has no notion of "this path is reserved for a future ID" and thus cannot prevent squatting on a not-yet-created path: [6](#0-5) [7](#0-6) 

The project's own test suite explicitly demonstrates the collision is exploitable: pre-creating a directory at `storage.DeriveReplicaPath(2)` before the "real" repository ID 2 is allocated causes the ensuing `CreateFork`/`CreateRepository` call to fail with `AlreadyExists`/`repository exists already`: [8](#0-7) [9](#0-8) 

### Impact Explanation
An unprivileged client with only ordinary repository-creation access (the same access level needed to push/import/fork a project) can repeatedly call `CreateRepository` with a `RelativePath` set to the predicted `DeriveReplicaPath`/`DerivePoolPath` output for the next several sequence values. Any legitimate `CreateFork`, `CreateObjectPool`, or `CreateRepository` call that is subsequently routed to one of those squatted IDs will fail with `AlreadyExists`, consuming the ID (the sequence does not roll back) and forcing GitLab/Rails to retry the operation (burning more IDs) or surface an error to the end user. This is a denial-of-service / resource-exhaustion class issue directly analogous to the front-run `initialize()` report: a cheap, unprivileged, predictable-target race that forces the legitimate operation to fail and be redone, at asymmetric cost to the attacker versus the operator/dev team.

### Likelihood Explanation
Likelihood is moderate-to-high in the Praefect-fronted deployment (the default for GitLab.com/most self-managed installs): the ID sequence is small, monotonic, and shared across all repository creations, so an attacker who can create even one throwaway repository/fork/pool learns roughly where the sequence currently stands and can squat several IDs ahead cheaply and repeatedly. No special privilege beyond ordinary repository-creation RPC access is required, and the target repository never needs to exist beforehand — it's a plain empty-directory creation.

### Recommendation
- Do not let `CreateRepository`'s client-supplied `RelativePath` silently succeed for paths matching the internally-reserved `@cluster/repositories/**` or `@cluster/pools/**` ID-derived pattern (`praefectRepositoryPathPrefix`/`praefectPoolPathPrefix`) unless the request specifically originates from Praefect's own internal repository-creation routing/reservation, or bind creation atomically to the `ReserveRepositoryID` call rather than deriving the disk path from a value predictable ahead of reservation.
- Alternatively, reserve the ID and pre-create/lock the target directory as part of the same reservation transaction so squatting cannot occur in the window between `ReserveRepositoryID` and the physical `repoutil.Create` call.
- Consider deriving the on-disk path from a value that isn't guessable ahead of time (e.g. include a random or reservation-transaction-scoped component instead of a bare small sequential integer hash) to remove the predictability that enables the race.

### Proof of Concept
1. As an ordinary client, call `RepositoryService.CreateRepository` (or any repository-creation RPC) once to observe roughly what repository ID space is in use (or simply infer via prior creations).
2. Compute `storage.DeriveReplicaPath(nextID)` for a handful of candidate future IDs, e.g. `@cluster/repositories/d4/73/2` for ID 2 (as shown in `storage.DeriveReplicaPath(2)`): [10](#0-9) 
3. Issue `CreateRepositoryRequest{Repository: {StorageName: ..., RelativePath: "@cluster/repositories/d4/73/2"}}` to create an empty repository squatting that exact path (this is exactly the mechanism used by the project's own regression test to force a collision): [11](#0-10) 
4. When Praefect later allocates repository ID 2 for a legitimate `CreateFork`/`CreateObjectPool`/`CreateRepository` from a real user, `RouteRepositoryCreation` derives the same colliding path and the subsequent `repoutil.Create` call fails with `AlreadyExists`/`repository exists already`, exactly as asserted in `create_fork_test.go`'s `TestCreateFork_targetExists` using the same `storage.DeriveReplicaPath(2)` collision path: [12](#0-11)

### Citations

**File:** internal/praefect/router_per_repository.go (L479-490)
```go
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

**File:** internal/gitaly/storage/repository_path_test.go (L12-20)
```go
func TestDeriveReplicaPath(t *testing.T) {
	require.Equal(t, "@cluster/repositories/6b/86/1", storage.DeriveReplicaPath(1))
	require.Equal(t, "@cluster/repositories/d4/73/2", storage.DeriveReplicaPath(2))
}

func TestDerivePoolPath(t *testing.T) {
	require.Equal(t, "@cluster/pools/6b/86/1", storage.DerivePoolPath(1))
	require.Equal(t, "@cluster/pools/d4/73/2", storage.DerivePoolPath(2))
}
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

**File:** internal/gitaly/service/repository/create_repository.go (L15-24)
```go
func (s *server) CreateRepository(ctx context.Context, req *gitalypb.CreateRepositoryRequest) (*gitalypb.CreateRepositoryResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	hash, err := git.ObjectHashByProto(req.GetObjectFormat())
	if err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
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

**File:** internal/gitaly/repoutil/create.go (L196-208)
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

**File:** internal/gitaly/service/repository/create_fork_test.go (L364-379)
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
```

**File:** internal/gitaly/service/repository/create_repository_test.go (L270-311)
```go
func TestCreateRepository_invalidArguments(t *testing.T) {
	t.Parallel()

	ctx := testhelper.Context(t)
	cfg, client := setupRepositoryService(t)

	preexistingRepo, _ := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{
		// This creates the first repository on the server. As this test can run with
		// Praefect in front of it, we'll use the next replica path Praefect will assign in
		// order to ensure this repository creation conflicts even with Praefect in front of
		// it.
		RelativePath: storage.DeriveReplicaPath(1),
	})

	for _, tc := range []struct {
		desc        string
		repo        *gitalypb.Repository
		expectedErr error
	}{
		{
			desc:        "missing repository",
			repo:        nil,
			expectedErr: structerr.NewInvalidArgument("%w", storage.ErrRepositoryNotSet),
		},
		{
			desc: "invalid storage",
			repo: &gitalypb.Repository{
				StorageName:  "does not exist",
				RelativePath: "foobar.git",
			},
			expectedErr: testhelper.ToInterceptedMetadata(structerr.NewInvalidArgument(
				"%w", storage.NewStorageNotFoundError("does not exist"),
			)),
		},
		{
			desc: "preexisting repository",
			repo: preexistingRepo,
			expectedErr: structerr.NewAlreadyExists("%s", testhelper.GitalyOrPraefect(
				"creating repository: repository exists already",
				"route repository creation: reserve repository id: repository already exists",
			)),
		},
```
