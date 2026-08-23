### Title
Repository-Creation Path Squatting Enables Permanent Denial-of-Service via `relative_path` Front-Running - (File: internal/gitaly/repoutil/create.go)

### Summary
`repoutil.Create()`, the shared helper backing `CreateRepository`, `CreateFork`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, and `CreateRepositoryFromSnapshot`, treats the user-supplied `relative_path` as an implicit uniqueness token: it only checks whether a filesystem entry already exists at that path and, if so, permanently rejects the request with `AlreadyExists`. Anyone who can predict or learn a target repository's `relative_path` before its legitimate owner creates it can pre-create an empty/garbage repository at that exact path, permanently blocking the legitimate creation — the same "front-run a supposedly-unique identifier before the legitimate holder submits it" bug class described in the external report about `validate_unique_tx`/`tx_hash`.

### Finding Description
`repoutil.Create` establishes uniqueness purely by checking that nothing exists on disk at `targetPath`, both in a "pre-lock" check and again after acquiring the repository lock: [1](#0-0) [2](#0-1) 

If the path is already occupied, the call fails with `structerr.NewAlreadyExists("repository exists already")` regardless of who created the pre-existing entry or when. This is exactly analogous to `validate_unique_tx`: the "uniqueness" is enforced only against the raw value supplied by (or derivable from) the caller, with no binding to a specific legitimate requester or nonce that only the true owner could know at submission time. Any actor that can issue a `CreateRepository`/`CreateFork`/`CreateRepositoryFromURL`/`CreateRepositoryFromBundle`/`CreateRepositoryFromSnapshot` RPC targeting an arbitrary `relative_path` on a reachable storage can win the race by squatting the path first.

This is reachable directly from RPC handlers that pass the caller-controlled `relative_path` straight through without any binding to project/user identity at the storage layer: [3](#0-2) [4](#0-3) 

The same class of check-then-act-on-existence is repeated in the transaction manager for the WAL-backed path, and yields the identical permanent `ErrRepositoryAlreadyExists` outcome: [5](#0-4) 

Once squatted, the legitimate creation attempt permanently fails — there is no retry-with-different-identifier recovery path baked into the protocol, mirroring the report's core complaint that legitimate operations are "permanently blocked" and users are "forced to retry."

### Impact Explanation
A successful squat causes permanent denial-of-service for repository creation at a specific target path: the legitimate `CreateRepository`/`CreateFork`/`CreateRepositoryFromURL`/`CreateRepositoryFromBundle`/`CreateRepositoryFromSnapshot` call will always return `AlreadyExists` until the squatted entry is manually cleaned up by an operator, since ordinary users have no way to reclaim or overwrite a path that already exists. Depending on how the upstream caller (e.g., GitLab Rails) derives `relative_path` (which is frequently deterministic, e.g., based on a monotonically increasing project ID under `@hashed/`), this can allow targeted disruption of specific upcoming project/fork/import operations. As with the referenced report, exploitability is bounded by how visible/predictable the target `relative_path` is to third parties before the legitimate create call lands.

### Likelihood Explanation
Exploitation requires only:
1. The ability to invoke a repository-creation RPC with an attacker-chosen `relative_path` (any authorized Gitaly/Praefect client has this).
2. Knowledge of, or ability to predict, the target `relative_path` before the legitimate creation request completes.

Both create RPCs are ordinary, user-reachable mutator RPCs with no special privilege beyond normal repository-creation authorization, so likelihood tracks entirely with how guessable/observable the target path is — the same "visibility-dependent" caveat the external report calls out for `tx_hash`.

### Recommendation
Do not rely solely on filesystem/path existence as the uniqueness gate for a caller-supplied identifier. Options:
- Bind repository creation to an authoritative, server-side allocated identifier (e.g., the internal repository ID / partition key already used by the WAL-based `TransactionManager`) rather than trusting a directly caller-suppliable relative path for uniqueness enforcement.
- Where `relative_path` must remain caller-supplied (e.g., derived deterministically by Rails), ensure the creating caller's authorization is checked against the *specific* target path/project mapping at creation time, so an unrelated caller cannot pre-empt a path they aren't authorized to create.
- Consider reserving the path (e.g., via the datastore-backed repository ID reservation already used by Praefect's `RouteRepositoryCreation`/`ErrRepositoryAlreadyExists` flow) prior to allowing any writer access, and audit all four Create* RPC paths for consistent enforcement.

### Proof of Concept
1. Determine or predict the `relative_path` that will be used for a soon-to-be-created repository (e.g., the next sequential `@hashed/...` or `@cluster/...` replica path, as illustrated by test helpers using `storage.DeriveReplicaPath(n)`): [6](#0-5) 
2. Before the legitimate owner's `CreateRepository`/`CreateFork` call, issue any repository-creation RPC (e.g., `CreateRepository`) targeting that same `storage_name`/`relative_path`.
3. When the legitimate request arrives, it fails permanently with `AlreadyExists`, matching the test-asserted behavior: [7](#0-6)

### Citations

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

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L1796-1800)
```go
			if transaction.repositoryCreation != nil && repositoryExists {
				return commitResult{error: ErrRepositoryAlreadyExists}
			} else if transaction.repositoryCreation == nil && !repositoryExists {
				return commitResult{error: storage.ErrRepositoryNotFound}
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

**File:** internal/gitaly/service/repository/create_fork_test.go (L364-371)
```go
			forkedRepo := &gitalypb.Repository{
				// As this test can run with Praefect in front of it, we'll use the next replica path Praefect will
				// assign in order to ensure this repository creation conflicts even with Praefect in front of it.
				// As the source repository created in the setup is the first one, this would get the repository
				// ID 2.
				RelativePath: storage.DeriveReplicaPath(2),
				StorageName:  repo.GetStorageName(),
			}
```
