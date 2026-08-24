### Title
Predictable, sequential replica/pool paths let an unprivileged actor pre-create the target directory Praefect will assign to a future repository/fork/object-pool, permanently denying that legitimate creation - (File: internal/praefect/router_per_repository.go, internal/gitaly/repoutil/create.go, internal/git/objectpool/create.go)

### Summary
This is an analog of the Sherlock AutoRoller finding: a deterministic identifier used to key a "create only if it does not already exist" resource can be pre-empted by an adversary, permanently denying the legitimate operation. In gitaly/Praefect, `RouteRepositoryCreation` derives the on-disk replica path (and object-pool path) purely from a monotonically increasing Postgres sequence value, and the underlying `repoutil.Create`/`objectpool.Create` functions unconditionally fail with `AlreadyExists`/`FailedPrecondition` if something already occupies that exact path.

### Finding Description
`PerRepositoryRouter.RouteRepositoryCreation` reserves the next repository ID from a plain auto-increment Postgres sequence and derives the on-disk path from it deterministically: [1](#0-0) 

The sequence itself is a bare `nextval('repositories_repository_id_seq')`, with no randomness: [2](#0-1) [3](#0-2) 

Because IDs are handed out sequentially to *any* project/fork/pool-creating action (an ordinary user creating a project, forking a public repository, or importing a repo all consume an ID), the next ID — and therefore the exact `DeriveReplicaPath`/`DerivePoolPath` value that will be used for the *next* legitimate repository or object-pool creation — is predictable by observing consumption of the sequence (e.g., by repeatedly triggering cheap project-creation/deletion cycles).

On the Gitaly side, both repository creation and object-pool creation refuse to proceed if anything already exists at the computed target path, and this check has no notion of "trusted" vs. "untrusted" caller — it's a hard failure: [4](#0-3) [5](#0-4) 

The project's own test suite explicitly demonstrates and relies on this determinism to construct a collision, confirming the reachability of the pattern: it precomputes `storage.DeriveReplicaPath(2)` — "the next replica path Praefect will assign" — and shows that a repository creation request targeting that exact path causes the *next* real creation to permanently fail with `AlreadyExists`: [6](#0-5) [7](#0-6) 

This mirrors the Sherlock bug class exactly: a deterministic, externally-observable key (maturity date / sequential repository ID) is used to gate creation of a unique resource (Space pool / object pool or repository directory), and any actor able to trigger the "create" code path for that key first (AutoRoller B / a crafted `CreateFork`/`CreateRepository`/`CreateObjectPool` call targeting the predicted path) can permanently block the legitimate holder of that key (AutoRoller A / the next real project, fork, or object pool) from ever succeeding, because the create path always retries with the *same eventually-reused, still-predictable* sequential scheme.

### Impact Explanation
An attacker with only ordinary repository-creation/forking privileges (no special trust) can cause repeated `AlreadyExists`/`FailedPrecondition` failures for other users' project creation, forking, or import operations by racing to occupy the predictable next replica/pool path. Because each retry after a collision consumes a *new* sequence value that is again predictable, the attack can be repeated indefinitely, resulting in a persistent, low-cost denial-of-service against repository/fork/object-pool creation for arbitrary victims sharing the same virtual storage.

### Likelihood Explanation
The prerequisite building blocks are all present and used unconditionally in production: sequential ID assignment (`ReserveRepositoryID`), deterministic path derivation (`DeriveReplicaPath`/`DerivePoolPath`), and unconditional existence checks that hard-fail with no privileged-caller carve-out. Exploitation requires only the ability to (a) observe/estimate sequence consumption rate, which any user triggering ordinary create/fork/import/delete actions on the shared virtual storage can influence and observe indirectly via timing/response, and (b) issue one more create request that targets the predicted path before the victim's request lands. This is a timing-race rather than a guaranteed hit, which lowers likelihood somewhat compared to the original AutoRoller bug (where the colliding maturity is exactly computable, not merely probable), but the core "no trusted-caller distinction, deterministic key, hard fail on any collision" pattern is directly analogous.

### Recommendation
- Do not rely purely on a shared, externally-influenceable monotonic counter to derive an on-disk storage path that gates uniqueness; consider randomizing part of the derived path (e.g., include a random component in addition to the sequence number) so the exact future path cannot be predicted by an unrelated actor.
- When `repoutil.Create`/`objectpool.Create` encounter an existing but unrelated file/directory at a freshly-reserved path (i.e., no matching Praefect DB record for that path/ID), treat it as a corrupted/squatted path and retry with a newly reserved ID instead of surfacing a terminal error to the legitimate caller.
- Add auditing/alerting when a `CreateRepository`/`CreateFork`/`CreateObjectPool` request lands on a path that was pre-existing but not tracked in the Praefect repository store, since this is a strong signal of exactly this kind of squatting attack.

### Proof of Concept
1. As an unprivileged user of a Praefect-fronted GitLab/Gitaly cluster, repeatedly trigger cheap repository-creating actions (create + immediately delete small projects, or repeated forks) to observe/consume the `repositories_repository_id_seq` and estimate the next value `N+1` that will be handed out.
2. Immediately before the victim's legitimate project-creation/fork/import request is processed, submit a request (e.g., `CreateFork`/`CreateRepository`) whose repository is routed to replica path `storage.DeriveReplicaPath(N+1)` (this is exactly what `internal/gitaly/service/repository/create_fork_test.go`'s `TestCreateFork_targetExists` constructs to prove the collision, using `storage.DeriveReplicaPath(2)` as "the next replica path Praefect will assign").
3. When the victim's request is subsequently routed and reserves ID `N+1`, `repoutil.Create`'s pre-lock `os.Stat` finds the path already occupied by the attacker's directory and returns `structerr.NewAlreadyExists("repository exists already")`, exactly as validated by `TestCreateFork_targetExists`'s expected error `structerr.NewAlreadyExists("creating fork: repository exists already")`, permanently denying that specific creation attempt.
4. Repeat for subsequent retries (each of which reserves a new, still-predictable sequence value) to sustain the denial-of-service.

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

**File:** _support/praefect-schema.sql (L286-294)
```sql
-- Name: repositories_repository_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.repositories_repository_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
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

**File:** internal/gitaly/service/repository/create_fork_test.go (L303-341)
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
