### Title
FetchIntoObjectPool permits fetching objects from an arbitrary, unrelated repository into an attacker-controlled object pool - (File: internal/gitaly/service/objectpool/fetch_into_object_pool.go)

### Summary
`FetchIntoObjectPool` takes two independent, caller-supplied repository references — `Origin` and `ObjectPool.Repository` — and only checks that they share the same `StorageName`. It never verifies that `Origin` is actually a member/fork of the target pool, mirroring the Sherlock bug class where a protocol accepted two user-supplied, logically-linked parameters (`underlying` asset and `pool`) without validating that they actually correspond to each other.

### Finding Description
`validateFetchIntoObjectPoolRequest` only asserts: [1](#0-0) 

There is no check that `Origin` is a pool member of `ObjectPool.Repository`, nor that the caller has any established relationship between the two repositories (e.g., via `objectpool.Link`/alternates). The handler then unconditionally fetches all refs/objects from `Origin` into the pool: [2](#0-1) 

This is the direct analog of the Illuminate bug: the "asset" (`ObjectPool` the attacker controls) and the "pool/source" (`Origin`, an arbitrary repository on the same storage) are supplied independently by the caller, and the only cross-field validation is a coarse, unrelated property (matching storage name) rather than proving that the two actually belong together (pool membership). An attacker who can invoke this RPC (or who controls a caller path that lets them choose `Origin` freely — e.g. any project on the same Gitaly storage/shard) can point `Origin` at a private repository they do not own, causing its objects to be fetched wholesale into a pool they do control, after which the objects are permanently readable from the attacker's own pool/fork.

### Impact Explanation
If reachable with attacker-controlled `Origin`, this is a cross-repository object disclosure: private commits/blobs/trees from a victim repository become present in a pool the attacker can read via their own fork, permanently and irrevocably (Git objects are content-addressed and can't be "unfetched"). This is analogous in severity to the theft described in the report, where mismatched-but-user-controlled inputs caused assets/data to flow to an unintended, attacker-benefiting destination.

### Likelihood Explanation
Likelihood hinges on which callers can invoke `FetchIntoObjectPool` with an arbitrary `Origin` value. In GitLab's normal flow, Rails is the only intended caller of this RPC (during fork/pool housekeeping) and is expected to only ever pair a project with its own fork-network pool; ordinary users do not call Gitaly RPCs directly, and Praefect additionally checks storage equality between the target and additional repositories in `mutatorStorageStreamParameters`/`rewrittenRepositoryMessage`. I could not conclusively determine (within the available tool budget) whether any authorization/ownership check exists upstream (in Rails or in Gitaly's request validation layer) that ties `Origin` to a specific pool before this RPC is invoked, nor whether pool membership is otherwise re-verified elsewhere (e.g., in `objectpool.Link`) prior to `FetchFromOrigin` being called. This uncertainty is significant: if such a binding check exists upstream, the practical likelihood is low/none; if it does not, the RPC-level surface is directly exploitable by anyone able to reach it with a crafted `Origin` field.

### Recommendation
In `validateFetchIntoObjectPoolRequest` (or in the handler before calling `FetchFromOrigin`), verify that `Origin` is an existing, legitimate member of `ObjectPool` — e.g., by confirming `Origin`'s alternates/objects-directory already reference the given pool (the same check used by `linkedToRepository` in `internal/git/objectpool/link.go`), rather than accepting any repository on the same storage. This closes the gap between "same storage" (a coarse, unrelated property) and "actually the correct paired repository" (the real invariant that must hold), matching the report's recommendation to check that user-supplied paired inputs actually correspond to one another.

### Proof of Concept
Conceptual PoC (not fully verified as end-to-end exploitable due to unresolved caller-authorization question noted above):
1. Attacker creates/owns `ObjectPool` repo `pool-attacker` on storage `default`.
2. Attacker calls `FetchIntoObjectPool` with:
   - `ObjectPool.Repository = {StorageName: "default", RelativePath: "pool-attacker.git"}`
   - `Origin = {StorageName: "default", RelativePath: "victim-private-repo.git"}`
3. `validateFetchIntoObjectPoolRequest` only compares `StorageName` ("default" == "default") and passes.
4. `objectPool.FetchFromOrigin(ctx, origin, ...)` fetches all refs/objects from `victim-private-repo.git` into `pool-attacker.git`.
5. Attacker reads objects from their own `pool-attacker` fork/repo (e.g., via `Repository.GetObjectDirectorySize`, `Commit.FindCommit`, `Blob.GetBlob`, etc.), exfiltrating the victim's private history.

### Citations

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L16-32)
```go
func (s *server) FetchIntoObjectPool(ctx context.Context, req *gitalypb.FetchIntoObjectPoolRequest) (*gitalypb.FetchIntoObjectPoolResponse, error) {
	if err := validateFetchIntoObjectPoolRequest(req); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}

	objectPool, err := objectpool.FromProto(ctx, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.housekeepingManager, req.GetObjectPool())
	if err != nil {
		return nil, structerr.NewInvalidArgument("object pool invalid: %w", err)
	}

	origin := s.localRepoFactory.Build(req.GetOrigin())

	if err := objectPool.FetchFromOrigin(ctx, origin, func(repo *gitalypb.Repository) *localrepo.Repo {
		return s.localRepoFactory.Build(repo)
	}); err != nil {
		return nil, structerr.NewInternal("%w", err)
	}
```

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L111-116)
```go
	originRepository, poolRepository := req.GetOrigin(), req.GetObjectPool().GetRepository()

	if originRepository.GetStorageName() != poolRepository.GetStorageName() {
		return errors.New("origin has different storage than object pool")
	}

```
