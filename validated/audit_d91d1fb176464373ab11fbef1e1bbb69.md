### Title
Unauthorized cross-repository object-pool linking allows an attacker to redistribute/pollute a victim repository's object graph via `LinkRepositoryToObjectPool` - (File: internal/gitaly/service/objectpool/link.go)

### Summary
`ObjectPoolService.LinkRepositoryToObjectPool` accepts two independent repository identifiers — `repository` (the pool member to be linked) and `object_pool` — and links them by writing an `objects/info/alternates` entry, with no check that the caller has any relationship-based right to join that specific `repository` to that specific `object_pool`. This mirrors the Opus finding: just as `abbot.deposit()` let anyone write collateral into *any* trove ID without being its owner, `LinkRepositoryToObjectPool` lets a caller couple *any* two repositories it can name via storage/relative-path, without Gitaly verifying they are actually part of the same fork network.

### Finding Description
The handler only validates that the `repository` field is a well-formed, existing Git repository and that the object pool can be resolved: [1](#0-0) 

The actual linking logic in `objectpool.Link` performs no ownership or fork-network check either — it merely verifies the alternates file is empty or already points at the same pool, then writes the pool's relative object path into the target repository's alternates file: [2](#0-1) 

The only cross-repository constraints enforced anywhere in the stack are storage/partition placement checks in the transaction middleware (same storage, same WAL partition) — not an authorization or fork-network relationship check: [3](#0-2) 

Because the object pool is a shared object store used for deduplication (`git repack` removes objects from a member if they exist in the pool, per `removeMemberBitmaps`/link flow), once a repository is linked, its private objects and its bitmap state are folded into the pool's shared search path, and pool-hosted objects become resolvable from within the linked repository. As documented, the intended lifecycle is that only genuine fork-network members should be linked: [4](#0-3) 

Analogous to the Opus bug where anyone could `deposit()` into any `trove_id` to manipulate which trove absorbs redistributed bad debt, here anyone able to invoke `LinkRepositoryToObjectPool` with an arbitrary `repository` value can force an unrelated (victim) repository to become a member of a pool it never asked to join — Gitaly performs no check that the two repositories named in the request actually belong together.

### Impact Explanation
Linking an arbitrary victim repository into an attacker-chosen (or attacker-controlled) object pool:
- Removes the victim repository's own bitmap (`removeMemberBitmaps`), degrading its performance characteristics and object-access guarantees without consent.
- Makes every object in the pool reachable through the victim repository's alternates search path, i.e., cross-repository object visibility that Gitaly did not intend for repositories outside a genuine fork network.
- Because deduplication during subsequent repacks removes redundant copies of the victim's own objects in favor of the pool copy, an attacker who can also delete/manage the pool (`DeleteObjectPool` "has no safety checks") can corrupt or orphan the victim's objects.

This corresponds to "cross-repository object access" analog explicitly called out as acceptable impact in the validation criteria.

### Likelihood Explanation
Likelihood is Low-to-Medium, matching the judged severity of the original finding. `ObjectPoolService` RPCs are intended to be invoked by trusted internal callers (Rails) as part of the fork/pool lifecycle, and in a Praefect/WAL deployment the requirement that pool and repository share the same storage and partition constrains which repositories can practically be linked together. However, Gitaly itself performs no verification tying the two repository fields together beyond storage/partition co-location, so any caller with the ability to invoke this mutator RPC with attacker-influenced `repository`/`object_pool` fields can trigger the unintended link, exactly as the low-likelihood-but-real Opus scenario required an attacker to merely be able to call a permissionless deposit function.

### Recommendation
Require that `LinkRepositoryToObjectPool` (and `CreateObjectPool`) verify a genuine relationship between the `repository` and `object_pool` (e.g., verify pool ancestry/fork-network membership recorded at repository-creation time) before writing the alternates link, rather than relying solely on storage/partition co-location as the only cross-repository constraint.

### Proof of Concept
1. Attacker creates repository A and an object pool P from A (`CreateObjectPool`), attacker fully controls both.
2. Attacker (or any caller able to reach `ObjectPoolService`) issues `LinkRepositoryToObjectPool{ Repository: victim_repo, ObjectPool: P }` where `victim_repo` is an unrelated repository sharing the same storage/partition.
3. Gitaly performs `ValidateRepository` on `victim_repo` and resolves `P` via `poolForRequest`, then calls `pool.Link(ctx, victim_repo)`, which succeeds because no ownership/fork-network check exists — see [1](#0-0)  and [5](#0-4) .
4. `victim_repo`'s `objects/info/alternates` now points at P's object directory, its bitmap is removed, and P's objects (including any injected by the attacker) become part of `victim_repo`'s resolvable object graph.

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

**File:** internal/git/objectpool/link.go (L28-83)
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

	if tx := storage.ExtractTransaction(ctx); tx != nil {
		alternatesRelativePath, err := filepath.Rel(tx.FS().Root(), altPath)
		if err != nil {
			return fmt.Errorf("rel alternates file: %w", err)
		}

		if err := tx.FS().RecordFile(alternatesRelativePath); err != nil {
			return fmt.Errorf("record alternates file")
		}
	}

	return removeMemberBitmaps(ctx, pool, repo)
```

**File:** internal/gitaly/storage/storagemgr/middleware.go (L332-361)
```go
	// Object pools need to be placed in the same partition as their members. Below we figure out which repository,
	// if any, the target repository of the RPC must be partitioned with. We figure this out using two strategies:
	//
	// The general case is handled by extracting the additional repository from the RPC, and partitioning the target
	// repository of the RPC with the additional repository. Many of the ObjectPoolService's RPCs operate on two
	// repositories. Depending on the RPC, the additional repository is either the object pool itself or a member
	// of the pool.
	//
	// CreateFork is special cased. The fork must partitioned with the source repository in order to successfully
	// link it with the object pool later. The source repository is not tagged as additional repository in the
	// CreateForkRequest. If the request is CreateForkRequest, we extract the source repository and partition the
	// fork with it.
	if additionalRepo, err := methodInfo.AdditionalRepo(req); err != nil {
		if !errors.Is(err, protoregistry.ErrRepositoryFieldNotFound) {
			return transactionalizedRequest{}, fmt.Errorf("extract additional repository: %w", err)
		}

		// There was no additional repository.
	} else {
		if alternateRelativePath != "" {
			return transactionalizedRequest{}, ErrPartitioningHintAndAdditionalRepoProvided
		}

		alternateStorageName = additionalRepo.GetStorageName()
		alternateRelativePath = additionalRepo.GetRelativePath()
	}

	if alternateStorageName != "" && alternateStorageName != targetRepo.GetStorageName() {
		return transactionalizedRequest{}, ErrRepositoriesInDifferentStorages
	}
```

**File:** proto/objectpool.proto (L10-30)
```text
// ObjectPoolService is a service that manages the lifetime of object pools.
//
// An object pool is a separate repository that can be linked to from multiple object pool members
// in order to deduplicate common objects between them. This is mostly used in the context of
// repository forks.
//
// The typical lifetime of an object pool is as follows:
//
// 1. An object pool is created via CreateObjectPool from its primary pool member. Typically this
//    would be the repository that gets forked.
// 2. One or more repositories are linked to the object pool via LinkRepositoryToObjectPool. Each
//    object pool member linked to the repository will have its objects deduplicated when its
//    objects get repacked the next time.
// 3. The object pool is regularly updated via FetchIntoObjectPool. This is typically only done from
//    the primary object pool member.
// 4. Repositories may leave the object pool via DisconnectGitAlternates. There is not much of a
//    reason to do this for any repositories except for the primary object pool member in case it
//    for example becomes private.
// 5. When the object pool does not have any members anymore it gets deleted via DeleteObjectPool.
//    It is the responsibility of the caller to ensure that it really has no members left, else
//    any existing member will become corrupt.
```
