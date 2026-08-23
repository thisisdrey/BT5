This confirms the analog. `DeriveReplicaPath` derives the on-disk replica path directly and deterministically from the sequential, publicly-observable `repositoryID` counter — the ID space is globally sequential (`repositories_repository_id_seq`, `START WITH 1 INCREMENT BY 1`) [1](#0-0) , and `DeriveReplicaPath` is a pure, non-secret function of that integer [2](#0-1) . Any user who creates a repository learns the current counter value and can trivially predict the replica path that the *next* sequential repository ID will resolve to (exactly as the test helpers do when they call `storage.DeriveReplicaPath(2)` to predict the following repository's path) [3](#0-2) . Gitaly's `repoutil.Create` only guards against concurrent creation via a stat-then-lock-then-stat check at that fixed path, not against pre-existing unrelated content placed there ahead of time [4](#0-3) [5](#0-4) .

### Title
Griefing of repository/fork/object-pool creation via predictable sequential replica-path squatting - (File: internal/gitaly/storage/repository_path.go, internal/praefect/datastore/repository_store.go, internal/gitaly/repoutil/create.go)

### Summary
Gitaly (behind Praefect) allocates repository identities from a single, globally sequential PostgreSQL sequence and derives the on-disk replica path from that integer via a public, keyless hash function. Because the sequence and derivation function are both observable and predictable, any unprivileged client that can trigger repository creation (push, fork, import, or a raw `CreateRepository`/`CreateFork`/`CreateObjectPool` RPC) can pre-compute the replica path of the *next* repository ID and race to create a bogus repository there first, causing the legitimate creation of another user's repository/fork/object pool to fail permanently with `AlreadyExists`.

### Finding Description
`ReserveRepositoryID` allocates IDs from a monotonically increasing Postgres sequence keyed only by `nextval('repositories_repository_id_seq')` [6](#0-5) . `RouteRepositoryCreation` then converts that reserved ID into the physical replica path with `storage.DeriveReplicaPath(id)` [7](#0-6) . `DeriveReplicaPath`/`deriveDiskPath` is a pure function: `sha256(strconv.Itoa(repositoryID))`'s first two bytes as subdirectories plus the raw ID as filename — no secret, salt, or per-tenant randomness is involved [8](#0-7) .

Any client that creates one repository can read back (or infer from timing) the current sequence value, and can therefore predict the replica path for `id+1`, `id+2`, etc., exactly as gitaly's own test suite does deliberately to avoid races: `storage.DeriveReplicaPath(2)` is used to compute "the next replica path Praefect will assign" before the real creation happens [3](#0-2) , and similar patterns recur in `create_repository_test.go`/`create_repository_from_snapshot_test.go`/`create_repository_from_url_test.go`.

On the Gitaly side, `repoutil.Create` (used by `CreateRepository`, `CreateFork`, `CreateObjectPool`, `CreateRepositoryFromURL`, `CreateRepositoryFromBundle`, `CreateRepositoryFromSnapshot`) only checks that the target path is currently absent before creating; it does not verify that the path is "reserved for" a particular caller/tenant:
```go
if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
    if err == nil {
        return structerr.NewAlreadyExists("repository exists already")
    }
    ...
}
``` [4](#0-3) 
and repeats the same check post-lock [5](#0-4) . If an attacker creates *any* directory/repository at the predicted future replica path before the legitimate operation runs, the legitimate `CreateRepository`/`CreateFork`/`CreateObjectPool` call will hit this check and fail with `AlreadyExists`, as shown by the existing tests for pre-seeded target directories `TestCreateFork_targetExists` [9](#0-8)  and `TestCreateRepositoryFromURL_existingTarget` [10](#0-9) .

At the Praefect metadata layer, the equivalent race is `ReserveRepositoryID`'s uniqueness check on `(virtual_storage, relative_path)`, which likewise returns `ErrRepositoryAlreadyExists` once any row occupies that ID/path combination [6](#0-5) . Because the sequence itself is global and increments on every reservation attempt (even failed/rolled-back ones due to `nextval` semantics), an attacker who repeatedly calls `CreateRepository` with self-chosen relative paths can also *deliberately advance* the sequence to align a future prediction, then instantly claim the ID Gitaly is about to hand to the victim.

This is a direct structural analog of the reported smart-contract bug: a globally shared, attacker-observable identifier/counter (`accountId` in the report; `repository_id` sequence in Gitaly) is used as the sole existence key for a resource, letting a bad actor front-run legitimate creation using a value they can predict, without needing to compromise the honest party at all.

### Impact Explanation
Any unprivileged client with RPC access to create repositories (a normal path exercised by pushes that trigger repo creation, project import, forking, or replication) can grief specific victims by:
1. Observing the counter is at `N` (e.g., after creating their own repository or via any operation reporting IDs/metadata).
2. Pre-creating junk repositories/directories at `DeriveReplicaPath(N+1)`, `DeriveReplicaPath(N+2)`, ... ahead of an expected fork/import/creation by another project.
3. Causing the victim's genuine `CreateRepository`/`CreateFork`/`CreateObjectPool`/`CreateRepositoryFromURL` call to fail with `AlreadyExists`, blocking that user's ability to create the corresponding project/fork indefinitely (or until manual intervention), with no benefit to the attacker beyond denial of service — matching the "Griefing" impact class in the original report.

### Likelihood Explanation
Exploitability requires only ordinary, unprivileged repository-creation access (the same access needed to push a new project, fork a repository, or trigger an import), and the derivation function/sequence numbering are entirely public and deterministic — no cryptographic secret or privileged information is needed to predict the target path. The attack is a straightforward TOCTOU race against a well-known target path rather than a brute-force search, making it comparatively easy relative to the reported analog.

### Recommendation
- Do not derive the sole on-disk existence key purely from a predictable, globally shared sequential counter; incorporate an unguessable, per-reservation component (e.g., a random UUID/nonce baked into the replica path, or a per-tenant namespace) so pre-creation at a predicted path cannot pre-empt a specific future repository.
- Alternatively/additionally, bind repository-path reservation directly to a specific caller/request at reservation time (e.g., transactionally reserve replica path and immediately materialize a placeholder that only the reserving transaction can complete), closing the window between `ReserveRepositoryID`/`DeriveReplicaPath` and the actual `repoutil.Create` filesystem move.
- Treat unexpected pre-existing content at a freshly reserved replica path as a fatal integrity error requiring operator intervention rather than a routine `AlreadyExists`, and add monitoring/alerting for repeated `AlreadyExists` failures on freshly reserved IDs, which would indicate active squatting.

### Proof of Concept
Illustrative sequence (aligning with `TestCreateFork_targetExists`'s pattern of pre-seeding the predicted path [11](#0-10) ):
```go
// Attacker learns/predicts the next repository ID, e.g. by having just created
// their own repository (ID N) or observing metadata RPCs.
predictedID := N + 1
targetPath := filepath.Join(storagePath, storage.DeriveReplicaPath(predictedID))

// Attacker pre-creates a directory at the predicted path before the victim's
// CreateFork/CreateRepository call executes.
os.MkdirAll(targetPath, mode.Directory)

// Victim performs a legitimate fork/import; Praefect reserves ID == predictedID
// and Gitaly's repoutil.Create finds the path already occupied:
_, err := client.CreateFork(ctx, &gitalypb.CreateForkRequest{
    Repository:       victimForkRepo, // resolves to replica path == targetPath
    SourceRepository: victimSourceRepo,
})
// err == structerr.NewAlreadyExists("creating fork: repository exists already")
```

### Citations

**File:** _support/praefect-schema.sql (L289-294)
```sql
CREATE SEQUENCE public.repositories_repository_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
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

**File:** internal/gitaly/storage/repository_path.go (L61-69)
```go
func deriveDiskPath(prefixDir string, repositoryID int64) string {
	hasher := sha256.New()
	// String representation of the ID is used to make it easier to derive the replica paths with
	// external tools. The error is ignored as the hash.Hash interface is documented to never return
	// an error.
	hasher.Write([]byte(strconv.FormatInt(repositoryID, 10)))
	hash := hasher.Sum(nil)
	return filepath.Join(prefixDir, fmt.Sprintf("%x/%x/%d", hash[0:1], hash[1:2], repositoryID))
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

**File:** internal/gitaly/service/repository/create_fork_test.go (L362-379)
```go
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

**File:** internal/gitaly/service/repository/create_repository_from_url_test.go (L110-163)
```go
func TestCreateRepositoryFromURL_existingTarget(t *testing.T) {
	t.Parallel()
	ctx := testhelper.Context(t)

	testCases := []struct {
		desc        string
		repoPath    string
		isDir       bool
		skipWithWAL string
	}{
		{
			desc:  "target is a directory",
			isDir: true,
		},
		{
			desc:  "target is a file",
			isDir: false,
			skipWithWAL: `
The transaction commit fails as the TransactionManager fails to initialize with the state
directory being a file. This is testing storage details rather than the RPC implementation
testing of this scenario should be left to the relevant package.
`,
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.desc, func(t *testing.T) {
			testhelper.SkipWithWAL(t, testCase.skipWithWAL)

			cfg, client := setupRepositoryService(t)

			importedRepo := &gitalypb.Repository{
				RelativePath: storage.DeriveReplicaPath(1),
				StorageName:  cfg.Storages[0].Name,
			}
			importedRepoPath := filepath.Join(cfg.Storages[0].Path, importedRepo.GetRelativePath())

			if testCase.isDir {
				require.NoError(t, os.MkdirAll(importedRepoPath, mode.Directory))
			} else {
				require.NoError(t, os.MkdirAll(filepath.Dir(importedRepoPath), mode.Directory))
				require.NoError(t, os.WriteFile(importedRepoPath, nil, mode.File))
			}
			t.Cleanup(func() { require.NoError(t, os.RemoveAll(importedRepoPath)) })

			req := &gitalypb.CreateRepositoryFromURLRequest{
				Repository: importedRepo,
				Url:        "https://gitlab.com/gitlab-org/gitlab-test.git",
			}

			_, err := client.CreateRepositoryFromURL(ctx, req)
			testhelper.RequireGrpcCode(t, err, codes.AlreadyExists)
		})
	}
```
