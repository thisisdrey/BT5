### Title
Missing Ownership/Scope Binding in `LinkRepositoryToObjectPool` Enables Cross-Repository Object Disclosure - ([File: internal/gitaly/service/objectpool/link.go])

### Summary
`LinkRepositoryToObjectPool` accepts two independent, caller-supplied identifiers — the `repository` to link and the `object_pool` to link it to — and performs the privileged linking operation after validating only that each is a syntactically/structurally valid Git repository. There is no check binding the `repository` to the specific `object_pool` (e.g. verifying the pool was actually created from/for that repository or its fork network). This mirrors the reported IDOR pattern: an identifier that should be authorized/bound at a higher trust layer (session, fork-network membership) is instead trusted as supplied by the caller.

### Finding Description
`LinkRepositoryToObjectPool` in `internal/gitaly/service/objectpool/link.go` does: [1](#0-0) 

It only calls `s.locator.ValidateRepository(ctx, repository)` (checks the path is a real git dir under a known storage) and `s.poolForRequest(ctx, req)`, then unconditionally calls `pool.Link(ctx, repo)`. Neither of these validations asserts that `repository` is actually a legitimate member of `object_pool`'s fork network — i.e., that the caller is authorized to associate these two specific repositories.

`Link()` itself (`internal/git/objectpool/link.go`) writes the pool's object directory into the repository's `objects/info/alternates` file: [2](#0-1) 

Once linked, Git will transparently read objects from the pool as if they were local to the member repository (that is the entire purpose of object pools, documented in `doc/object_pools.md`). The RPC's protobuf definition confirms both fields are independently, directly caller-supplied via the request message, with no session-level binding between them: [3](#0-2) 

This is structurally identical to the `getPrivyUserId()` bug class: two sensitive identifiers (`privyUserId` / here, `repository` + `object_pool`) that determine the scope of a privileged operation (fund transfer / here, object access grant) are supplied directly by the caller instead of being derived from and validated against an authoritative, server-side relationship (API-key-to-user mapping / here, fork-network or pool-ownership record).

### Impact Explanation
If this RPC is reachable with a `repository` and `object_pool` pair that do not actually belong to the same fork network (e.g. a caller-controlled/attacker-owned repository and an arbitrary pool whose storage/relative path is known or guessed), Gitaly will link the attacker's own accessible repository to that pool. From that point on, any object present in the pool — potentially derived from private repositories that share the pool — becomes readable through the attacker's own repository via standard Git/RPC operations (e.g. `git cat-file`, `GetBlob`, `TreeEntry`), because the alternates mechanism makes pool objects appear as native objects of the linked repository. This is a concrete cross-repository object disclosure, matching the same "any caller with a valid credential can act on/read from a resource they don't own" impact class as the original IDOR (there, unauthorized fund movement; here, unauthorized object read across repository/project boundaries).

### Likelihood Explanation
Reachability depends on how strictly the calling layer (e.g. Rails via internal API) constrains the `repository`/`object_pool` pairing before invoking this RPC. Within Gitaly itself — the only in-scope surface for this analysis — there is no defense-in-depth check preventing an arbitrary pairing, so any caller able to invoke `ObjectPoolService` RPCs with attacker-influenced repository/pool identifiers (e.g. through a crafted internal API call, replay, or a client bypassing upstream authorization logic) can trigger the disclosure. This is a Medium-likelihood finding because it requires knowledge of the target pool's `storage_name`/`relative_path`, but no additional secrets, and Gitaly performs no independent verification.

### Recommendation
Enforce the repository↔pool relationship at the Gitaly RPC-handler level rather than trusting the caller-supplied pair: before calling `pool.Link`, verify that `object_pool` is the pool actually associated with `repository`'s fork network/origin (e.g., via a `GetObjectPool`-style lookup based on the repository's own alternates ancestry, or a persisted binding created at `CreateObjectPool`/`CreateFork` time), and reject the request if the supplied pool does not match. This mirrors the report's recommendation to bind trust to a server-side authoritative source (database/session), not to caller-supplied identifiers.

### Proof of Concept
```
# Attacker controls repo_attacker (any repository they can access via a valid gRPC/API credential).
# Attacker learns/guesses the storage_name + relative_path of pool_victim (e.g. an object pool
# created for a private fork network the attacker does not have direct object access to).

grpcurl -plaintext -d '{
  "object_pool": {
    "repository": { "storage_name": "default", "relative_path": "@pools/aa/bb/pool_victim.git" }
  },
  "repository": { "storage_name": "default", "relative_path": "repo_attacker.git" }
}' gitaly:9999 gitaly.ObjectPoolService/LinkRepositoryToObjectPool

# repo_attacker/objects/info/alternates now points at pool_victim's object directory.
# Attacker can now fetch/read any object contained in pool_victim through repo_attacker,
# e.g. via CommitService/GetBlob or `git cat-file -p <oid>` against repo_attacker.
```

Note: I could not fully verify within the indexed code whether an upstream layer (outside Gitaly, e.g. GitLab Rails) enforces the repository/pool relationship before calling this RPC in production deployments — that check, if it exists, lives outside this repository's scope. Within Gitaly's own code, no such binding is present.

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

**File:** internal/git/objectpool/link.go (L25-42)
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
```

**File:** proto/objectpool.proto (L123-129)
```text
// LinkRepositoryToObjectPoolRequest is a request for the LinkRepositoryToObjectPool RPC.
message LinkRepositoryToObjectPoolRequest {
  // object_pool is the object pool to which the repository shall be linked to.
  ObjectPool object_pool = 1 [(additional_repository)=true];
  // repository is the repository that shall be linked to the object pool.
  Repository repository = 2 [(target_repository)=true];
}
```
