### Title
Permissionless repository-creation RPCs allow target-path squatting/front-running DoS — ([File: internal/gitaly/repoutil/create.go])

### Summary
The reported bug is a griefing/DoS pattern: a shared, permissionless resource is guarded by a strict "must not already be occupied" check, and any unprivileged caller can cheaply pre-occupy the resource ahead of a legitimate actor, causing the legitimate operation to fail. The same pattern exists in Gitaly's repository-creation path: `repoutil.Create` performs a check-then-act existence test on a caller-controlled target path and refuses to create the repository if anything already occupies that path, with no mechanism to retry against an alternate slot or to detect/evict a griefing occupant.

### Finding Description
`repoutil.Create` (used by `CreateRepository`, `CreateFork`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, `CreateRepositoryFromSnapshot`, and `CreateObjectPool`) first stats the target repository path and fails with `AlreadyExists` if anything is already there: [1](#0-0) 

It repeats essentially the same check a second time after acquiring the repository lock, to close the race window between the first check and the transactional move: [2](#0-1) 

The target path is derived directly from the `Repository.RelativePath` supplied in the RPC request (see `CreateFork`, `CreateRepositoryFromURL`), which is attacker-influenceable/predictable in common flows (e.g. a fork or new project whose destination namespace/slug is deterministic, or — behind Praefect — a `replica_path` derived from a monotonically incrementing repository-ID sequence via `storage.DeriveReplicaPath`): [3](#0-2) [4](#0-3) [5](#0-4) 

Just like the reported deposit function's `require(amount <= capacity - totalSupply)` reverting the whole transaction once a griefer's dust deposit consumes the remaining capacity, `Create`'s "must not exist" invariant reverts the whole repository-creation RPC (`AlreadyExists`) the moment a griefer occupies the same target path first — with no refund/retry/redirect logic, exactly mirroring the report's recommended-but-missing fix ("refund/redirect the user instead of reverting").

### Impact Explanation
Any client permitted to call repository-creation RPCs (fork, import, project creation) for a namespace can pre-empt a specific, predictable target path with a cheap, minimal creation call (or by racing the lock file, see `repoutil.Lock`), permanently denying the legitimate creation of that repository at that path and returning `AlreadyExists` errors to the victim. This is a functional DoS of a core Gitaly handler group, matching the "Medium impact — core functionality DOSed" characterization in the source report, without requiring privileged access to the target repository itself. [6](#0-5) 

### Likelihood Explanation
The attack is cheap (a single lightweight `git init`/temp-repo creation or a `.lock` file race) and requires only the ability to invoke a repository-creation RPC targeting the same relative/replica path as the victim — no elevated privileges, no leaked tokens, no MITM. The main constraint is predicting the exact target path/ID ahead of the victim's request, which is realistic in flows with deterministic destination naming (forks, imports, sequential replica-path derivation).

### Recommendation
- Do not let unauthenticated/unrelated callers race a shared, predictable target path: tie path reservation to an authorization check performed atomically with reservation, or make repository IDs/paths unguessable until commit.
- Where feasible, treat "target already exists but wasn't created by an authorized flow" distinctly from a genuine collision, and consider allowing retried creation to reuse a freshly reserved slot, similar to Recommendation in the report.
- Add rate limiting on repository-creation RPCs per client to make squatting non-trivial to repeat.

### Proof of Concept
1. Attacker learns/predicts the `RelativePath` (or, behind Praefect, the next `repository_id`/`replica_path` via `storage.DeriveReplicaPath`) that a victim's upcoming `CreateFork`/`CreateRepository`/`CreateRepositoryFromURL` call will target.
2. Attacker issues a `CreateRepository` (or any RPC funneling into `repoutil.Create`) for that exact path first; `os.Stat(targetPath)` finds nothing, so the attacker's cheap repo gets created.
3. Victim's legitimate request now hits the same check in `repoutil.Create` (`internal/gitaly/repoutil/create.go:96-104` or `:202-208`) and is rejected with `AlreadyExists`, exactly as demonstrated by the existing test `TestCreateFork_targetExists`: [7](#0-6)

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

**File:** internal/gitaly/service/repository/create_fork.go (L16-30)
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
```

**File:** internal/gitaly/storage/repository_path.go (L45-50)
```go
// DeriveReplicaPath derives a repository's disk storage path from its repository ID. The repository ID
// is hashed with SHA256 and the first four hex digits of the hash are used as the two subdirectories to
// ensure even distribution into subdirectories. The format is @cluster/repositories/ab/cd/<repository-id>.
func DeriveReplicaPath(repositoryID int64) string {
	return deriveDiskPath(praefectRepositoryPathPrefix, repositoryID)
}
```

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

**File:** internal/gitaly/repoutil/lock.go (L22-52)
```go
func Lock(ctx context.Context, logger log.Logger, locator storage.Locator, repository storage.Repository) (func(), error) {
	path, err := locator.GetRepoPath(ctx, repository, storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	// Create the parent directory in case it doesn't exist yet.
	if err := os.MkdirAll(filepath.Dir(path), mode.Directory); err != nil {
		return nil, structerr.NewInternal("create directories: %w", err)
	}

	// We're somewhat abusing this file writer given that we simply want to assert that
	// the target directory doesn't exist and isn't created while we want to move the
	// new repository into place. We thus only use the locking semantics of the writer,
	// but will never commit it.
	locker, err := safe.NewLockingFileWriter(path)
	if err != nil {
		return nil, err
	}

	unlock := func() {
		if err := locker.Close(); err != nil {
			logger.WithError(err).ErrorContext(ctx, "closing repository locker failed")
		}
	}

	if err := locker.Lock(); err != nil {
		unlock()

		return nil, err
	}
```

**File:** internal/gitaly/service/repository/create_fork_test.go (L303-323)
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
```
