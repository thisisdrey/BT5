### Title
Predictable, sequential replica-path derivation allows unprivileged frontrunning of repository creation, permanently blocking a victim's `CreateRepository`/`CreateFork`/`ReplicateRepository` call - (File: internal/gitaly/storage/repository_path.go, internal/gitaly/repoutil/create.go)

### Summary
`repoutil.Create` (used by `CreateRepository`, `CreateFork`, `ReplicateRepository`, `CreateObjectPool`, etc.) resolves a target on-disk path from `storage.Repository`/`storage.DeriveReplicaPath`, checks that the path does not yet exist, and unconditionally fails with `AlreadyExists` if something is already there — both before and after taking the repository lock [1](#0-0) [2](#0-1) . When Praefect is in front of Gitaly, the replica path a repository will occupy is derived deterministically from a monotonically-increasing repository ID (`storage.DeriveReplicaPath`, keyed off `ReserveRepositoryID`) rather than from client-chosen data. This is exactly the pattern exercised in `TestCreateFork_targetExists`, which pre-creates content at `storage.DeriveReplicaPath(2)` before issuing `CreateFork`, and observes that the fork request fails hard with `"creating fork: repository exists already"` [3](#0-2) .

### Finding Description
The reported Permit2Proxy bug class is: a two-step operation depends on a shared, uniquely-identified resource (the EIP-2612 signature/allowance); an unprivileged third party can consume that resource ahead of the legitimate caller purely because the resource's identity is knowable in advance, causing the legitimate operation to permanently fail even though the attacker gains nothing.

The same shape exists in Gitaly's repository-creation path:
- `repoutil.Create` treats "target path already occupied" as a hard, non-recoverable failure (`structerr.NewAlreadyExists`) rather than verifying whether the pre-existing content is benign/foreign and safely superseding it [4](#0-3) .
- Under Praefect, the identity of the target (`replica_path`) that a not-yet-created repository will get is `storage.DeriveReplicaPath(repositoryID)`, and `repositoryID` comes from a sequential reservation (`ReserveRepositoryID`) [5](#0-4) . Because the ID sequence is predictable/observable (e.g. by creating throwaway repositories and watching the counter advance, or by knowing approximate creation order), an unprivileged user can compute the replica path that will be assigned to a subsequent legitimate `CreateRepository`/`CreateFork`/`ReplicateRepository` call before it happens.
- If a user manages to have a repository (or any file/directory) already occupy that exact relative path in the storage backend by the time the victim's create call reaches Gitaly, `repoutil.Create`'s pre-lock and post-lock existence checks both fail the operation permanently with `AlreadyExists` [2](#0-1) . On the Praefect side, `CreateRepository` on the datastore likewise surfaces `ErrRepositoryAlreadyExists` and is turned into a hard gRPC `AlreadyExists` for the finalizer [6](#0-5) .

This mirrors the report's root cause precisely: a permissionless, one-shot "claim" of a resource identified by predictable data lets an attacker who has no legitimate interest in the transaction cause it to fail, without any benefit to themselves beyond griefing the victim.

### Impact Explanation
An attacker cannot corrupt or read another user's repository content, gain elevated privileges, or execute arbitrary code — the impact is limited to denial of service: legitimate `CreateRepository`, `CreateFork`, `CreateObjectPool`, or `ReplicateRepository` calls for a targeted victim can be made to fail with `AlreadyExists`, forcing retries or manual intervention and potentially disrupting fork/import/replication workflows at scale (repeated griefing against, e.g., a project import pipeline or fork-of-forks scenario). This matches the "low risk"/DoS-of-a-handler classification in the source report rather than a data-integrity or auth-bypass issue.

### Likelihood Explanation
Likelihood is constrained by two factors that need further verification with source access: (1) how predictable/observable the repository-ID sequence actually is to an arbitrary tenant (cross-project ID enumeration would typically require some visibility into unrelated projects' creation cadence), and (2) whether an ordinary gRPC client can place arbitrary content at a *specific* not-yet-used replica path without already possessing storage-level write access (client requests generally go through repository-relative-path validation and are not free-form on Praefect-managed storages). Given the codebase's own test (`TestCreateFork_targetExists`) demonstrates the failure mode exists and is reachable purely through target-path collision, the underlying mechanism is confirmed, but the exact means by which an *unprivileged* external actor reliably wins the race in a running multi-tenant deployment is not fully provable from the indexed code alone.

### Recommendation
- In `repoutil.Create`, when the target path is found to already exist, before returning `AlreadyExists`, verify whether the existing directory is a residual/foreign artifact versus a fully-formed, successfully committed repository; if it is not a valid Git directory (or does not match expected identity/state), clean it up and proceed, analogous to the report's recommendation of tolerating a "harmless" pre-existing state rather than always failing hard.
- Avoid deriving replica paths from a low-entropy, externally inferable sequence; consider path derivation salted or namespaced so it is not practically predictable by unrelated tenants.
- Ensure `ReserveRepositoryID`/`CreateRepository` treat only exact metadata conflicts (not passive filesystem-content collisions) as `AlreadyExists`, decoupling logical repository existence from raw path occupancy where possible.

### Proof of Concept
The existing unit test demonstrates the mechanics: a target replica path is pre-populated with arbitrary content (empty dir, non-empty dir, or a plain file) before `CreateFork` is called for a repository expected to land at that exact `storage.DeriveReplicaPath(2)`; the RPC then fails with `AlreadyExists`/`repository exists already` [3](#0-2) . In a real deployment, an attacker would need only to (a) determine or bracket the repository ID about to be assigned to a victim's create/fork/replicate operation and (b) cause any content to exist at the corresponding relative path beforehand (e.g., via their own accepted repository-creation call consuming that ID, or via direct storage access if available) to reproduce the same permanent failure against the victim's legitimate request.

### Citations

**File:** internal/gitaly/repoutil/create.go (L90-104)
```go
) error {
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

**File:** internal/gitaly/repoutil/create.go (L191-208)
```go
	unlock, err := Lock(ctx, logger, locator, repository)
	if err != nil {
		return fmt.Errorf("locking repository: %w", err)
	}
	defer unlock()

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

**File:** internal/gitaly/service/repository/create_fork_test.go (L303-381)
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
```

**File:** internal/praefect/datastore/repository_store.go (L101-103)
```go
	// ReserveRepositoryID reserves an ID for a repository that is about to be created and returns it. If a repository already
	// exists with the given virtual storage and relative path combination, an error is returned.
	ReserveRepositoryID(ctx context.Context, virtualStorage, relativePath string) (int64, error)
```

**File:** internal/praefect/coordinator.go (L1173-1189)
```go
			if err := c.rs.CreateRepository(ctx,
				repositoryID,
				virtualStorage,
				targetRepo.GetRelativePath(),
				replicaPath,
				primary,
				updatedSecondaries,
				outdatedSecondaries,
				repositorySpecificPrimariesEnabled,
				storeAssignments,
			); err != nil {
				if errors.Is(err, datastore.ErrRepositoryAlreadyExists) {
					return structerr.NewAlreadyExists("%w", err)
				}

				return fmt.Errorf("create repository: %w", err)
			}
```
