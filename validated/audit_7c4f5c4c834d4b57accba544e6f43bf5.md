### Title
Unrestricted repeated `LinkRepositoryToObjectPool` / `DisconnectGitAlternates` cycles allow resource-exhaustion arbitrage of a shared object pool - (File: internal/gitaly/service/objectpool/util.go)

### Summary
The external report's bug class is "repeated deposit/withdraw with no lock-up or ownership check against a shared pool, letting the caller extract value/impose cost with no restriction." The closest reachable analog in Gitaly is the `ObjectPoolService`'s `LinkRepositoryToObjectPool` and `DisconnectGitAlternates` RPCs, which let any caller who can name a repository and a target/pool repository repeatedly attach and detach that repository from a shared object pool, with no check that the repository actually is (or ever was) a real member/fork relative of that pool, and no throttling of the cycle.

### Finding Description
`poolForRequest` only validates that the object pool path is a well-formed pool directory (`objectpool.ErrInvalidPoolDir`) — it performs no verification that the `repository` parameter of `LinkRepositoryToObjectPool` has any prior relationship to the pool being linked to: [1](#0-0) 

`Link()` itself only checks whether the repository is *already* linked to a *different* pool (which fails), but happily links a brand-new, unrelated repository to any existing pool, deleting the repository's bitmap files as a side effect (`removeMemberBitmaps`): [2](#0-1) [3](#0-2) 

`DisconnectGitAlternates` performs the inverse, expensive operation: it hard-links every object that is part of the object pool into the (previously linked) repository, then removes the alternates file, then runs `git-fsck(1)` to validate the result: [4](#0-3) 

Because `LinkRepositoryToObjectPool` and `DisconnectGitAlternates` are unauthenticated, unthrottled MUTATOR RPCs that can be invoked repeatedly against the same target/pool pair, and because linking/unlinking is not gated behind any real "membership lock" (no lock-up period, no verification that the repository is a genuine fork of the pool's origin, no rate limit), an ordinary caller can:
1. Call `LinkRepositoryToObjectPool` to attach a large repository (or many repositories) to a shared pool, immediately triggering bitmap deletion on the member.
2. Call `DisconnectGitAlternates` immediately afterward, forcing Gitaly to hard-link every object in the (potentially very large, shared) object pool into the member repository and then run a full `git-fsck` over it.
3. Repeat the cycle continuously.

This directly mirrors the reported bug class: a "deposit" (Link) followed immediately by a "withdraw" (Disconnect) that pulls the full economic/computational value out of a shared pool with no cooldown, and can be repeated indefinitely to arbitrage the shared resource — in this case CPU/disk I/O of the storage node rather than LP token rewards, but the same root cause (no lock-up/ownership gate on entry-then-immediate-exit from a shared pool).

### Impact Explanation
Repeated Link/Disconnect cycles against a shared, potentially very large object pool force expensive hard-linking of the entire pool's object set plus a full `git-fsck` on every disconnect, and bitmap deletion/repack costs on every link. An attacker who can issue RPCs for repositories/pools they control (e.g., via their own project's repository and any object pool they can name, since there is no ownership binding validated by `poolForRequest`) can repeatedly trigger this expensive I/O/CPU work at will, causing storage node resource exhaustion (DoS) that affects the shared storage node and other repositories collocated on it. It can also corrupt the intended deduplication invariants of the pool subsystem (unlinking non-member repositories, deleting bitmaps unnecessarily).

### Likelihood Explanation
The RPCs are reachable by any client authorized to call GitLab-facing Gitaly RPCs (e.g., through the internal API on behalf of a project owner/maintainer, which is an "ordinary user" tier relative to Gitaly's threat model, not a privileged Gitaly operator). No lock-up, cooldown, or membership-history check exists in `poolForRequest`/`Link`/`Disconnect` to prevent an immediate link-then-unlink cycle, so exploitation requires only scripting repeated calls — likelihood is high given the ease of reproduction and total absence of rate limiting/relationship checks in the traced code path.

### Recommendation
- In `poolForRequest`/`Link`, verify that the target `repository` is actually derived from (or previously an object-pool member of) the pool's origin before permitting linking, rather than only validating pool directory shape.
- Add a minimum dwell/lock-up time between `LinkRepositoryToObjectPool` and `DisconnectGitAlternates` for the same repository/pool pair, or otherwise refuse an immediate disconnect right after a link.
- Apply RPC-level rate limiting/concurrency limits per repository or per pool for `LinkRepositoryToObjectPool` and `DisconnectGitAlternates` to bound the cost of repeated hard-link + `git-fsck` cycles.

### Proof of Concept
1. Caller creates (or is given) a repository `R` and identifies any object pool `P` reachable through their storage (`CreateObjectPool`/known pool relative path).
2. Caller invokes `LinkRepositoryToObjectPool(pool=P, repository=R)` — succeeds even though `R` has no prior relationship with `P`, per the validation shown in `poolForRequest` and `Link()`: [1](#0-0) 
3. Caller immediately invokes `DisconnectGitAlternates(repository=R)`, forcing Gitaly to hard-link the full pool `P` object set into `R` and run `git-fsck`.
4. Caller repeats steps 2–3 in a loop with no delay, since no lock-up or cooldown check exists in the traced code, repeatedly consuming storage-node CPU/disk I/O for hard-linking and fsck of potentially large shared pools.

### Citations

**File:** internal/gitaly/service/objectpool/util.go (L35-49)
```go
func (s *server) poolForRequest(ctx context.Context, req PoolRequest) (*objectpool.ObjectPool, error) {
	pool, err := objectpool.FromProto(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.housekeepingManager, req.GetObjectPool())
	if err != nil {
		if errors.Is(err, objectpool.ErrInvalidPoolDir) {
			return nil, errInvalidPoolDir
		}

		if errors.Is(err, objectpool.ErrInvalidPoolRepository) {
			return nil, structerr.NewFailedPrecondition("%w", err)
		}

		return nil, structerr.NewInternal("%w", err)
	}

	return pool, nil
```

**File:** internal/git/objectpool/link.go (L19-52)
```go
// Link calls the non-receiver method version of Link with the parameters
// injected from the object pool.
func (o *ObjectPool) Link(ctx context.Context, repo *localrepo.Repo) error {
	return Link(ctx, o.Repo, repo, o.txManager)
}

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

**File:** internal/git/objectpool/link.go (L83-84)
```go
	return removeMemberBitmaps(ctx, pool, repo)
}
```

**File:** proto/objectpool.proto (L60-78)
```text
  // DisconnectGitAlternates will disconnect the object pool member from its object pool. It will:
  //
  // 1. Link all objects from the object pool into the member repository. This essenitally
  //    reduplicates previously-duplicated objects so that the repository will continue to function
  //    after being unlinked.
  // 2. Remove the alternates link to the object pool.
  // 3. Perform a consistency check to assert that the repository is indeed fully functional after
  //    unlinking it from its pool. If the consistency check fails the alternates link is restored
  //    an the RPC fails.
  //
  // If successful, the object pool member is disconnected from the object pool and does not depend
  // on it anymore.
  //
  // This RPC does not return an error in case the repository is not linked to any object pool.
  rpc DisconnectGitAlternates(DisconnectGitAlternatesRequest) returns (DisconnectGitAlternatesResponse) {
    option (op_type) = {
      op: MUTATOR
    };
  }
```
