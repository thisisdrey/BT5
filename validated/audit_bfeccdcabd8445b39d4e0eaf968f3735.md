### Title
`LinkRepositoryToObjectPool` allows linking an object pool to itself, creating a self-referencing `alternates` file - (File: `internal/git/objectpool/link.go`)

### Summary
The Cally report shows that failing to reject "self as parameter" (using the contract's own address as the vault's underlying token) lets a caller create a self-referential object whose later operations always fail, permanently locking funds. The Gitaly analog is `Link()` in `internal/git/objectpool/link.go`, invoked by the `ObjectPoolService.LinkRepositoryToObjectPool` RPC, which writes a Git alternates file pointing the target repository at the pool's object directory without ever checking whether the "repository" being linked *is* the pool itself.

### Finding Description
`LinkRepositoryToObjectPool` (`internal/gitaly/service/objectpool/link.go`) only validates that the target `repository` exists via `s.locator.ValidateRepository`, then resolves the pool via `poolForRequest` and calls `pool.Link(ctx, repo)`: [1](#0-0) 

`Link()` itself (`internal/git/objectpool/link.go`) computes the relative alternates path between `pool` and `repo` and, if not already linked, writes it directly to the repo's `objects/info/alternates` file: [2](#0-1) 

Nowhere in this path is there a check that `repo`'s relative path differs from `pool`'s relative path. If a caller supplies the pool repository itself as the `Repository` field of `LinkRepositoryToObjectPoolRequest` (i.e. `pool == repo`), `getRelativeObjectPath` will compute a path that resolves back into the pool's own `objects` directory, and the alternates file ends up referencing itself.

Gitaly does have a guard for this exact self-reference condition — `storage.ErrAlternatePointsToSelf`, enforced in `internal/gitaly/storage/storagemgr/partition_assigner.go` (confirmed by the test case "alternate pointing to self fails" in `internal/gitaly/storage/storagemgr/partition_assigner_test.go`, lines 271-275) — but that check only runs lazily when the transaction manager resolves partition assignments for a repository, not synchronously inside `objectpool.Link()`. This means the self-referencing alternates file can be *written to disk* by an ordinary `LinkRepositoryToObjectPool` call before any consistency check ever runs, exactly mirroring the Cally pattern where the vulnerable state is created because there's no upfront `require(token != address(this))`-style check at the point of resource creation/linking.

### Impact Explanation
Once a repository's `objects/info/alternates` file points at itself, any Git operation that resolves alternates (`fsck`, `repack`, `cat-file`, transaction snapshotting, etc.) either loops/fails or the repository silently believes it has an alternate object store that never actually augments its object graph. Because Gitaly's `RemoveMember`/dissociation logic and partition-assignment logic assume a well-formed, non-circular alternates chain, a self-referencing pool corrupts state for that storage location and can render the repository object pool unusable — a DoS analogous to the "ETH locked forever" outcome in the Cally bug (a broken, self-referential internal linkage that permanently degrades a resource for legitimate future callers).

### Likelihood Explanation
Likelihood is Medium: `LinkRepositoryToObjectPool` is a real RPC reachable by any client that can create/administer object pools (used internally by GitLab for repository forking/pool management), and nothing prevents a caller from passing the pool repository's own coordinates as the `Repository` field of the link request. It requires a specific, deliberate parameter choice (as in the original Cally case, which was judged Medium severity for the same reason — it needs a "precise and niche" caller action) rather than being triggerable purely by normal usage.

### Recommendation
Add an explicit check in `LinkRepositoryToObjectPool` (or in `objectpool.Link`) that rejects the request when `repository`'s relative path equals the pool's relative path (i.e., reuse/hoist the existing `storage.ErrAlternatePointsToSelf` check to run synchronously at link time, not only lazily during partition assignment).

### Proof of Concept
1. Create an object pool `P` via `CreateObjectPool` from some origin repository.
2. Call `LinkRepositoryToObjectPool` with `ObjectPool = P` and `Repository = P` (the pool's own repository coordinates).
3. `Link()` writes `P/objects/info/alternates` referencing `P`'s own objects directory, creating a self-referencing alternates chain that is only caught later (if at all) by `storagemgr`'s partition assigner, at which point the repository/pool is already in a corrupted, inconsistent state. [3](#0-2)

### Citations

**File:** internal/gitaly/service/objectpool/link.go (L10-27)
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
```

**File:** internal/git/objectpool/link.go (L28-70)
```go
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

	if err := transaction.CommitLockedFile(ctx, txManager, alternatesWriter); err != nil {
		return fmt.Errorf("committing alternates: %w", err)
	}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner_test.go (L271-275)
```go
		{
			desc:                    "alternate pointing to self fails",
			memberAlternatesContent: []byte("../objects"),
			expectedError:           storage.ErrAlternatePointsToSelf,
		},
```
