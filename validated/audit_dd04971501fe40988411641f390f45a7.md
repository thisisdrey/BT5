### Title
Cross-repository object disclosure via unrestricted `LinkRepositoryToObjectPool` alternate linking - (File: internal/gitaly/service/objectpool/link.go)

### Finding Description
`LinkRepositoryToObjectPool` only validates that `Repository` is a syntactically valid repository via `s.locator.ValidateRepository` and that `ObjectPool` resolves to a valid pool directory via `s.poolForRequest`/`objectpool.FromProto`. It performs no check that the caller is authorized to associate these two specific repositories, nor that `Repository` is not itself already an object pool. [1](#0-0) 

The actual link operation simply writes the target pool's object directory path into the `Repository`'s `objects/info/alternates` file: [2](#0-1) 

Once this alternates file is in place, any Git process (and any Gitaly RPC) operating on `Repository` transparently gains read access to every object stored in the linked `ObjectPool`, as documented in Gitaly's own quarantine/alternates design docs (alternates act as a search path Git checks when looking up an object): [3](#0-2) 

This is structurally analogous to the Caviar `PrivatePool` bug: there, any NFT (including a "container" `Factory` NFT representing another pool) could be deposited/linked into an unrelated pool, and that link then enabled privileged pool-internal operations (`flashloan`, `withdraw`, `buy`/`sell`) to reach into and drain the referenced pool. In Gitaly, the "link" primitive (`objects/info/alternates`) is the analogous connective mechanism, and `LinkRepositoryToObjectPool` is the RPC that creates it without verifying that the two repositories are supposed to be related (e.g. that the pool was actually created from that repository, or that the caller has read access to the pool's contents).

Additionally, Gitaly's own alternate-chaining protections are inconsistent across code paths:
- The classic (non-WAL) alternates resolver `git/dirs.go` explicitly *permits* alternate chains up to depth 5: [4](#0-3) 
- The transactional/WAL partition assigner explicitly *rejects* any alternate-of-an-alternate (`ErrAlternateHasAlternate`) and self-pointing alternates (`ErrAlternatePointsToSelf`): [5](#0-4) 

This means that on non-WAL storage, nothing in `LinkRepositoryToObjectPool` or `objectpool.Link` prevents linking one object pool repository as a "member" into a second, unrelated object pool, producing a chain of alternates (Pool A → Pool B) up to 5 levels deep. Any repository that is itself a member of Pool A would then transitively gain read access to Pool B's objects — objects it was never meant to see — mirroring how depositing a "pool-representing" NFT into another pool let an attacker recursively reach assets that were supposed to be isolated.

### Impact Explanation
If an actor can invoke `LinkRepositoryToObjectPool` with a `Repository` they influence and an `ObjectPool` they should not have access to (e.g., because they can reference an arbitrary pool path on the same storage, or because they can get a pool linked into a broader pool they control), they gain read access to all Git objects held in that pool — commits, blobs, trees from potentially private/unrelated repositories that share the same object pool storage. Because object pools are keyed off attacker-visible relative paths and the RPC does no ownership/ACL check itself (that is assumed to be enforced entirely by the caller, e.g. Rails), any weakness or bypass upstream, or any direct/crafted RPC access to this Gitaly service, results in disclosure of arbitrary repository content across repository boundaries — a confidentiality break of the kind the "storage escape / cross-repository object access" bug class in the given ruleset targets.

### Likelihood Explanation
Exploitability depends on whether an ordinary user's action (fork creation, import flow) or a crafted RPC field can supply arbitrary `Repository`/`ObjectPool` pairs to this RPC without the normal ownership relationship Rails is expected to enforce (source repo → its own pool). Because Gitaly itself performs no cross-repository authorization check and instead relies entirely on path validity (`ValidateRepository`, `IsPoolRepository`) rather than a relationship check (e.g., that the pool was created from this exact repository, tracked via `GetObjectPool`), likelihood is Medium: it requires either direct/crafted access to the `ObjectPoolService` RPC surface or a flaw in the calling layer's parameter construction, similar to how the Caviar issue required a private pool operator to accept a Factory-minted NFT, categorized there as Medium severity because it depends on how the composability primitive is actually used.

### Recommendation
Add a check in `LinkRepositoryToObjectPool` (and in the lower-level `objectpool.Link`) that:
1. Rejects linking a repository into a pool it wasn't intended for by validating an explicit relationship (e.g., recording and checking the original source repository the pool was created from, similar to how `CreateObjectPool` records `origin`), rather than trusting caller-supplied pairs blindly.
2. Rejects linking when `Repository` is itself a pool repository (`storage.IsPoolRepository`) unless that is an explicitly supported/audited configuration, preventing pool-into-pool chaining.
3. Enforces the same "no alternate-of-an-alternate" invariant that `partition_assigner.go` already enforces for WAL, uniformly in the non-WAL `git/dirs.go` alternates resolution path, rather than allowing a 5-level chain.

### Proof of Concept
1. Create Pool A from repository R1 via `CreateObjectPool(Origin=R1, ObjectPool=PoolA)`.
2. Create Pool B from a private/unrelated repository R2 via `CreateObjectPool(Origin=R2, ObjectPool=PoolB)`, where the attacker does not have direct object-read access to R2/PoolB.
3. Attacker calls `LinkRepositoryToObjectPool(Repository=PoolA.Repository, ObjectPool=PoolB)` (or, on non-WAL storage, `Repository=R1_fork_linked_to_PoolA`) — the RPC succeeds because only path validity is checked: [6](#0-5) 
4. `objects/info/alternates` under Pool A now points at Pool B's objects directory: [7](#0-6) 
5. Any repository already linked to Pool A (or the attacker fetching/reading directly from Pool A) can now resolve and read objects that physically only exist in Pool B/R2, via the alternates chain traversed by `altObjectDirs`: [8](#0-7)

### Citations

**File:** internal/gitaly/service/objectpool/link.go (L10-28)
```go
func (s *server) LinkRepositoryToObjectPool(ctx context.Context, req *gitalypb.LinkRepositoryToObjectPoolRequest) (*gitalypb.LinkRepositoryToObjectPoolResponse, error) {
	repository := req.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	pool, err := s.poolForRequest(ctx, req)
	if err != nil {
		return nil, err
	}

	repo := s.localRepoFactory.Build(repository)

	if err := pool.Link(ctx, repo); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}

	return &gitalypb.LinkRepositoryToObjectPoolResponse{}, nil
}
```

**File:** internal/git/objectpool/link.go (L25-52)
```go
// Link will link the given repository to the object pool. This is done by writing the object pool's
// path relative to the repository into the repository's "alternates" file. This does not trigger
// deduplication, which is the responsibility of the caller.
func Link(ctx context.Context, pool, repo *localrepo.Repo, txManager transaction.Manager) (returnedErr error) {
	altPath, err := repo.InfoAlternatesPath(ctx)
	if err != nil {
		return err
	}

	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return err
	}

	linked, err := linkedToRepository(ctx, pool, repo)
	if err != nil {
		return err
	}

	if linked {
		// When the repository is already linked to the repository, cast a vote to ensure the
		// repository is consistent with the other replicas.
		if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("repository linked")), voting.Synchronized); err != nil {
			return fmt.Errorf("vote on linked repository: %w", err)
		}

		return nil
	}
```

**File:** internal/git/objectpool/link.go (L54-66)
```go
	alternatesWriter, err := safe.NewLockingFileWriter(altPath)
	if err != nil {
		return fmt.Errorf("creating alternates writer: %w", err)
	}
	defer func() {
		if err := alternatesWriter.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("closing alternates writer: %w", err)
		}
	}()

	if _, err := io.WriteString(alternatesWriter, expectedRelPath); err != nil {
		return fmt.Errorf("writing alternates: %w", err)
	}
```

**File:** doc/object_pools.md (L10-12)
```markdown
The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.
```

**File:** internal/git/dirs.go (L45-50)
```go
func altObjectDirs(ctx context.Context, logger log.Logger, storagePrefix, objDir string, depth int) ([]string, error) {
	const maxAlternatesDepth = 5 // Taken from https://github.com/git/git/blob/v2.23.0/sha1-file.c#L575
	if depth > maxAlternatesDepth {
		logger.WithField("objdir", objDir).WarnContext(ctx, "ignoring deeply nested alternate object directory")
		return nil, nil
	}
```

**File:** internal/git/dirs.go (L64-93)
```go
	dirs := []string{objDir}

	alternates, err := os.ReadFile(filepath.Join(objDir, "info", "alternates"))
	if os.IsNotExist(err) {
		return dirs, nil
	}
	if err != nil {
		return nil, err
	}

	for _, newDir := range strings.Split(string(alternates), "\n") {
		if len(newDir) == 0 || newDir[0] == '#' {
			continue
		}

		if !filepath.IsAbs(newDir) {
			newDir = filepath.Join(objDir, newDir)
		}

		if !strings.HasPrefix(newDir, storagePrefix) {
			return nil, alternateOutsideStorageError(newDir)
		}

		nestedDirs, err := altObjectDirs(ctx, logger, storagePrefix, newDir, depth+1)
		if err != nil {
			return nil, err
		}

		dirs = append(dirs, nestedDirs...)
	}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L317-349)
```go
func (pa *partitionAssigner) getAlternatePartitionID(ctx context.Context, relativePath string, recursiveCall bool, partitionHint storage.PartitionID) (storage.PartitionID, error) {
	alternate, err := gitstorage.ReadAlternatesFile(filepath.Join(pa.storagePath, relativePath))
	if err != nil {
		return 0, fmt.Errorf("read alternates file: %w", err)
	}

	if recursiveCall {
		// recursive being true indicates we've arrived here through another repository's alternate.
		// Repositories in Gitaly should only have a single alternate that points to the repository's
		// pool. Chains of alternates are unexpected and could go arbitrarily long, so fail the operation.
		return 0, storage.ErrAlternateHasAlternate
	}

	// The relative path should point somewhere within the same storage.
	alternateRelativePath, err := storage.ValidateRelativePath(
		pa.storagePath,
		// Take the relative path to the repository, not 'repository/objects'.
		filepath.Dir(
			// The path in alternates file points to the object directory of the alternate
			// repository. The path is relative to the repository's own object directory.
			filepath.Join(relativePath, "objects", alternate),
		),
	)
	if err != nil {
		return 0, fmt.Errorf("validate relative path: %w", err)
	}

	if alternateRelativePath == relativePath {
		// The alternate must not point to the repository itself. Not only is it non-sensical
		// but it would also cause a dead lock as the repository is locked during this call
		// already.
		return 0, storage.ErrAlternatePointsToSelf
	}
```
