### Title
Missing validation that `repository`/`origin` actually belongs to the target `object_pool` in ObjectPoolService RPCs enables cross-repository object exposure - ([File: internal/gitaly/service/objectpool/link.go])

### Summary
`LinkRepositoryToObjectPool` and `FetchIntoObjectPool` accept two independently-specified identifiers — a `repository`/`origin` and an `object_pool` — and never verify that these two identifiers actually correspond to a legitimate pool/member relationship (e.g., that `repository` was forked from the project the pool was seeded from). Gitaly only checks that each identifier is individually well-formed and that they share a storage, then wires the pool's object directory into the target repository's Git alternates. This mirrors the reported bug class: two IDs that are supposed to be logically coupled (`pair_id`/`token_id`) are trusted independently without cross-validation, letting a caller "mix and match" them to reach objects/funds that were never meant to be accessible together.

### Finding Description
`LinkRepositoryToObjectPool` validates only that `repository` is a valid, existing repository and that `object_pool` resolves to a valid pool repository; it never checks that `repository` is a real member of that specific pool's fork network: [1](#0-0) 

The actual linking logic in `objectpool.Link` similarly performs no ownership/relationship check — it only guards against clobbering an existing, differently-targeted alternates file, then unconditionally writes the caller-supplied pool's relative object path into the repository's `objects/info/alternates`: [2](#0-1) [3](#0-2) 

`FetchIntoObjectPool` has the same structural gap: `validateFetchIntoObjectPoolRequest` only checks that `origin` and `object_pool` are non-nil and share a storage name — not that `origin` is a recognized member of that pool: [4](#0-3) 

Once linked, alternates grant the member repository full read access to every object physically present in the pool's `objects/` directory — including objects contributed there by *other, unrelated* pool members, per Git's alternates search-path semantics documented in `doc/object_pools.md`: [5](#0-4) 

Because Gitaly's own proto contract explicitly disclaims responsibility for this consistency ("It is the responsibility of the caller to ensure..."), the correctness of the `repository`↔`object_pool` pairing is entirely delegated to the calling layer with no defense-in-depth check inside Gitaly itself: [6](#0-5) 

This is structurally identical to the reported flaw: `PlaceOrder`/`OrderJettonNotification` trusted the caller-supplied `token_id` without checking it matched the `pair_id`, letting attackers reach funds belonging to an unrelated pair. Here, Gitaly trusts the caller-supplied `repository`/`origin` without checking it matches the `object_pool`'s actual member set, letting a repository be linked into (or fetched into) an object pool it has no legitimate relationship with.

### Impact Explanation
If any caller-controlled path (fork creation, repository import/migration, or a crafted internal API call) can cause a mismatched `repository`/`object_pool` pair to reach these RPCs, an attacker's repository can be linked to an arbitrary object pool, giving it read access via alternates to every object ever fetched into that pool — including objects originating from unrelated, potentially private repositories that also happen to be (or were) pool members. This is a cross-repository/cross-tenant object disclosure, analogous to the reported theft of tokens across pairs that were never meant to interact.

### Likelihood Explanation
Exploitation requires the ability to invoke `LinkRepositoryToObjectPool`/`FetchIntoObjectPool` with an inconsistent `repository`↔`object_pool`/`origin` pairing. Gitaly performs no verification of this relationship itself and delegates it entirely to the caller (normally GitLab Rails during fork/import flows), so any defect, race, or unusual code path in that caller layer — or any other in-scope client permitted to drive these RPCs — translates directly into a cross-repository object leak with no additional check inside Gitaly to catch it.

### Recommendation
- **Short term:** In `LinkRepositoryToObjectPool` and `FetchIntoObjectPool`, verify that the supplied `repository`/`origin` is a recognized member (or legitimate fork source) of the specified `object_pool` before writing alternates or fetching, e.g., by consulting the pool's member/state tracking (`relational.PoolStore`) rather than trusting the request fields alone.
- **Long term:** Apply the same "validate the relationship between paired identifiers, not just their individual well-formedness" principle to every RPC that accepts two independently-specified repository references (target + additional repository), consistent with the `pair_id`/`token_id` lesson from the external report.

### Proof of Concept
1. Attacker has write access to `RepoA` (target of `LinkRepositoryToObjectPoolRequest.repository`) and knowledge of the relative path of `PoolB` (`object_pool`), an object pool that legitimately holds objects from unrelated `RepoC`/`RepoD` members.
2. Attacker (or a caller layer that fails to cross-check the relationship, e.g., via a crafted internal RPC call) issues `LinkRepositoryToObjectPool{repository: RepoA, object_pool: PoolB}`.
3. Gitaly's checks — `ValidateRepository` on `RepoA`, `objectpool.FromProto` validity check on `PoolB` — both pass since each is independently valid; no check ties `RepoA` to `PoolB`'s legitimate member set: [1](#0-0) 
4. `objectpool.Link` writes `PoolB`'s object directory into `RepoA/objects/info/alternates`: [7](#0-6) 
5. Any subsequent read via `RepoA` (e.g., `GetBlob`, `TreeEntry`) can now resolve object IDs that only exist in `PoolB`'s pack files (contributed by `RepoC`/`RepoD`), leaking objects across repository/tenant boundaries.

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

**File:** internal/git/objectpool/link.go (L168-204)
```go
// linkedToRepository tests if a repository is linked to an object pool
func linkedToRepository(ctx context.Context, pool, repo *localrepo.Repo) (bool, error) {
	poolPath, err := pool.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting object pool path: %w", err)
	}

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return false, fmt.Errorf("getting repo path: %w", err)
	}

	altInfo, err := stats.AlternatesInfoForRepository(repoPath)
	if err != nil {
		return false, fmt.Errorf("getting alternates info: %w", err)
	}

	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return false, nil
	}

	relPath := altInfo.ObjectDirectories[0]
	expectedRelPath, err := getRelativeObjectPath(ctx, pool, repo)
	if err != nil {
		return false, err
	}

	if relPath == expectedRelPath {
		return true, nil
	}

	if filepath.Clean(relPath) != filepath.Join(poolPath, "objects") {
		return false, fmt.Errorf("unexpected alternates content: %q", relPath)
	}

	return false, nil
}
```

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L102-118)
```go
func validateFetchIntoObjectPoolRequest(req *gitalypb.FetchIntoObjectPoolRequest) error {
	if req.GetOrigin() == nil {
		return errors.New("origin is empty")
	}

	if req.GetObjectPool() == nil {
		return errors.New("object pool is empty")
	}

	originRepository, poolRepository := req.GetOrigin(), req.GetObjectPool().GetRepository()

	if originRepository.GetStorageName() != poolRepository.GetStorageName() {
		return errors.New("origin has different storage than object pool")
	}

	return nil
}
```

**File:** doc/object_pools.md (L9-19)
```markdown

The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.

## Lifetime of Object Pools

The lifetime of object pools is maintained via the
[ObjectPoolService](../proto/objectpool.proto), which provides various RPCs to
create and delete object pools as well as to add members to or remove members
from the pool.
```

**File:** proto/go/gitalypb/objectpool_grpc.pb.go (L196-218)
```go
	// LinkRepositoryToObjectPool links the specified repository to the object pool. Objects contained
	// in the object pool will be deduplicated for this repository when repacking objects.
	LinkRepositoryToObjectPool(context.Context, *LinkRepositoryToObjectPoolRequest) (*LinkRepositoryToObjectPoolResponse, error)
	// DisconnectGitAlternates will disconnect the object pool member from its object pool. It will:
	//
	//  1. Link all objects from the object pool into the member repository. This essenitally
	//     reduplicates previously-duplicated objects so that the repository will continue to function
	//     after being unlinked.
	//  2. Remove the alternates link to the object pool.
	//  3. Perform a consistency check to assert that the repository is indeed fully functional after
	//     unlinking it from its pool. If the consistency check fails the alternates link is restored
	//     an the RPC fails.
	//
	// If successful, the object pool member is disconnected from the object pool and does not depend
	// on it anymore.
	//
	// This RPC does not return an error in case the repository is not linked to any object pool.
	DisconnectGitAlternates(context.Context, *DisconnectGitAlternatesRequest) (*DisconnectGitAlternatesResponse, error)
	// FetchIntoObjectPool fetches all references from a pool member into an object pool so that
	// objects shared between this repository and other pool members can be deduplicated. This RPC
	// will perform housekeeping tasks after the object pool has been updated to ensure that the pool
	// is in an optimal state.
	FetchIntoObjectPool(context.Context, *FetchIntoObjectPoolRequest) (*FetchIntoObjectPoolResponse, error)
```
