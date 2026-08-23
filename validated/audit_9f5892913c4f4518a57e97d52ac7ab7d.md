### Title
Repository-path squatting bypasses `repoutil.Create`'s existence check, permanently blocking legitimate repository/fork creation - (File: `internal/gitaly/repoutil/create.go`)

### Summary
`repoutil.Create`, which backs `CreateRepository`, `CreateFork`, and `ReplicateRepository`, determines whether a target repository may be created solely by checking with `os.Stat` whether *anything* already exists at the deterministically-derivable target path. Because the target `RelativePath` is caller-supplied and, for hashed storage, is a deterministic function of a predictable, sequentially-increasing project ID, an attacker can pre-create a directory/file at a victim's future repository path at negligible cost, permanently causing the victim's legitimate `CreateRepository`/`CreateFork` call to fail with `AlreadyExists`. This mirrors the CREATE2 address-squatting griefing pattern from the Wildcat report: an attacker precomputes a not-yet-used resource identifier and cheaply occupies it before the legitimate actor can claim it, permanently denying them access to that identifier.

### Finding Description
`repoutil.Create` performs two "existence" checks purely based on filesystem presence: [1](#0-0) 

and again after acquiring the lock: [2](#0-1) 

If `os.Stat(targetPath)` succeeds for *any* reason (an empty directory, a stray file, or a partially-formed directory left by an unrelated operation), the call unconditionally fails with `structerr.NewAlreadyExists("repository exists already")`, regardless of whether the entry is an actual Git repository.

The target path is derived directly from the caller-supplied `Repository.RelativePath` field, with no check that the caller is entitled to that specific path or that a real repository would ever legitimately occupy it: [3](#0-2) [4](#0-3) 

The codebase's own test suite confirms this behavior is triggered by merely placing an empty directory (or even a single stray file) at the target path before the RPC runs, with no need to construct a valid Git repository: [5](#0-4) 

Because GitLab's `@hashed` storage layout computes a repository's relative path as a deterministic hash of the project ID, and project IDs are sequentially assigned and observable, an attacker who can trigger any repository-creation RPC with an attacker-chosen relative path (e.g., via project creation/fork/import flows that route through `CreateRepository`/`CreateFork`) can precompute the path that the *next* sequential project will be assigned and squat it ahead of time — exactly the "precompute the deployment address, occupy it cheaply, force the real actor's deployment to fail" pattern from the report, just realized against `os.Stat`-based path occupancy instead of an EIP-1052 `codehash` check.

### Impact Explanation
A victim whose project/repository is about to be created at a predictable relative path can be permanently denied the ability to create that repository: `CreateRepository`/`CreateFork` will always return `AlreadyExists` for that path once squatted, since nothing in `repoutil.Create` ever attempts to reclaim or validate an unexpected non-Git directory occupying the target. This is a persistent denial of service against a core, frequently-invoked Gitaly handler (repository/fork creation), directly analogous to the "permanently lock a registered borrower out of ... functionality" and "grief market deployments" impacts described in the source report (assessed there as valid, if lower-severity, griefing findings).

### Likelihood Explanation
Exploitation cost is minimal (creating an empty directory or file is essentially free, analogous to the 1-wei transfer in the original report) and requires no elevated privileges — only the ability to trigger a repository-creation code path with a chosen relative path before the legitimate creation happens, and knowledge/prediction of the target path (feasible when paths are derived from sequential, hashable identifiers). This is comparable in likelihood to the "grief market deployment" scenario in the source report, which was still deemed valid and confirmed by the project maintainers.

### Recommendation
- Do not treat arbitrary filesystem presence at the target path as proof that a legitimate repository already exists. Instead, validate that the existing entry is actually a valid Git repository (e.g., via `storage.ValidateGitDirectory`) before returning `AlreadyExists`; if it is not a valid Git directory, treat it as reclaimable and remove/replace it (similar to the recovery logic already present in `internal/gitaly/service/repository/replicate.go`'s `create()` function, which renames away an invalid pre-existing directory rather than failing outright).
- Consider binding repository creation to an authorization/ownership token issued by the layer that assigns the relative path (e.g., Rails), so that Gitaly does not accept creation at an arbitrary attacker-chosen relative path independent of legitimate provisioning.

### Proof of Concept
1. Attacker predicts the relative path that will be assigned to the next repository to be created (e.g., the deterministic hashed path for the next sequential project ID, or any relative path they expect a victim to use).
2. Attacker invokes any RPC path that causes Gitaly to create a directory/file at that relative path (e.g., calling `CreateRepository`/`CreateFork` for their own use with a colliding `RelativePath`, or otherwise causing an empty directory to exist there), as demonstrated by the test setup: [6](#0-5) 
3. When the victim's legitimate `CreateRepository`/`CreateFork` call for that path executes, `repoutil.Create`'s `os.Stat` check at [7](#0-6)  succeeds and the call permanently fails with `AlreadyExists`, blocking the victim from ever creating their repository at that path through this code path.

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

**File:** internal/gitaly/service/repository/create_repository.go (L15-19)
```go
func (s *server) CreateRepository(ctx context.Context, req *gitalypb.CreateRepositoryRequest) (*gitalypb.CreateRepositoryResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository, storage.WithSkipRepositoryExistenceCheck()); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
```

**File:** internal/gitaly/service/repository/create_fork.go (L16-27)
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
