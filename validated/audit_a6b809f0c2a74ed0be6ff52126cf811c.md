### Title
Unrestricted `LinkRepositoryToObjectPool` allows cross-repository object disclosure via arbitrary alternates linking - (File: internal/gitaly/service/objectpool/link.go)

### Summary
`OmoRouter.transfer()` failed because it moved value between arbitrary vault shares without checking that the caller was authorized or that the transfer belonged to a legitimate relationship. The Gitaly analog is `ObjectPoolService.LinkRepositoryToObjectPool`, which lets a caller link *any* target `repository` to *any* `object_pool` repository supplied in the request, without validating that the two repositories have any legitimate forking/ownership relationship. Once linked, the target repository gains an `objects/info/alternates` pointer into the pool's object store, exposing every object in the pool (including unreachable/private blobs and commits) to anyone who can read the linked repository.

### Finding Description
The handler simply validates that `repository` is a well-formed, existing repository, resolves the `object_pool` argument via `objectpool.FromProto`, and then calls `pool.Link()`: [1](#0-0) 

`poolForRequest` only checks that the referenced pool path is a *valid pool repository* (i.e. has the on-disk pool marker) — it never checks that the pool is the one originally derived from `repository`, nor that the caller is authorized to associate the two: [2](#0-1) 

`objectpool.FromProto` similarly only validates that the given path *is* a pool repository (or in a temp dir during creation); it performs no cross-check against the `repository` being linked: [3](#0-2) 

`Link()` then unconditionally writes the pool's object directory into the target repo's `objects/info/alternates` file, granting the target repository read access to every object stored in the pool: [4](#0-3) 

Because Git resolves any object ID present in an alternate object directory, once linked, a caller can fetch/cat-file any blob, tree, or commit that exists in the pool — even ones unreachable from any ref in the pool and never intended to be shared — simply by knowing or guessing the object ID. The design documentation confirms pools are meant to be linked only to legitimate fork members, but the RPC itself enforces no such relationship: [5](#0-4) 

### Impact Explanation
An actor with the ability to invoke `LinkRepositoryToObjectPool` for a repository they control (a standard capability exposed through GitLab's fork/project pipeline, or directly via the gRPC API to Gitaly) can link that repository to an arbitrary object pool belonging to an unrelated project. This grants unauthorized read access to potentially private objects (source code, credentials committed to history, etc.) stored in that pool, constituting cross-repository object disclosure — analogous in severity to the unrestricted `transfer()` allowing unauthorized movement of value between unrelated vaults.

### Likelihood Explanation
Exploitation requires only knowledge/guessability of a target pool's `storage_name`/`relative_path` (which follow a predictable, sequential ID-based naming scheme for Praefect-routed pools, as seen in `internal/praefect/get_object_pool.go`) and the ability to issue one authenticated RPC call with attacker-controlled `repository` and `object_pool` fields — no special privilege beyond ordinary repository-mutating access is checked by the handler itself.

### Recommendation
`LinkRepositoryToObjectPool` should verify that the given `object_pool` is actually derived from (or otherwise authorized to be associated with) the target `repository` before performing the link — e.g., by checking a recorded pool-origin relationship (as tracked by `ScanPoolMetadata`/`PoolStore`) rather than trusting the caller-supplied pool path outright. Absent such a check, any authorization decision must be pushed to and strictly enforced by the calling layer (GitLab Rails), and Gitaly should reject links to pools with no verifiable relationship to the target repository.

### Proof of Concept
1. Attacker controls repository `A` (`storage/relative-path-A`).
2. Attacker learns or guesses the relative path of an unrelated private object pool `P` (`storage/@pools/xx/yy/pool-id`), e.g. via sequential/predictable pool IDs exposed by replication or `GetObjectPool` responses.
3. Attacker calls `LinkRepositoryToObjectPool(repository=A, object_pool=P)`.
4. `link.go` validates only that `A` exists and `P` is *a* valid pool — not that `P` belongs to `A`'s fork family — then writes `P`'s object directory into `A/objects/info/alternates`.
5. Attacker can now run `git cat-file -p <object-id>` inside `A` for any object ID present in pool `P`, disclosing objects from the unrelated private repository that seeded `P`.

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

**File:** internal/gitaly/service/objectpool/util.go (L35-50)
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
}
```

**File:** internal/git/objectpool/pool.go (L46-75)
```go
// FromProto returns an object pool object from its Protobuf representation. This function verifies
// that the object pool exists and is a valid pool repository.
func FromProto(
	ctx context.Context,
	logger log.Logger,
	locator storage.Locator,
	gitCmdFactory gitcmd.CommandFactory,
	catfileCache catfile.Cache,
	txManager transaction.Manager,
	housekeepingManager housekeepingmgr.Manager,
	proto *gitalypb.ObjectPool,
) (*ObjectPool, error) {
	poolPath, err := locator.GetRepoPath(ctx, proto.GetRepository(), storage.WithRepositoryVerificationSkipped())
	if err != nil {
		return nil, err
	}

	if !storage.IsPoolRepository(proto.GetRepository()) {
		// When creating repositories in the ObjectPool service we will first create the
		// repository in a temporary directory. So we need to check whether the path we see
		// here is in such a temporary directory and let it pass.
		tempDir, err := locator.TempDir(proto.GetRepository().GetStorageName())
		if err != nil {
			return nil, fmt.Errorf("getting temporary storage directory: %w", err)
		}

		if !strings.HasPrefix(poolPath, tempDir) {
			return nil, ErrInvalidPoolDir
		}
	}
```

**File:** internal/git/objectpool/link.go (L25-66)
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

**File:** doc/object_pools.md (L1-19)
```markdown
# Object Pools

When creating forks of a repository, most of the objects for forked repository
and the repository it forked from are shared. Storing those shared objects
multiple times is a waste of disk space and also of CPU time, given that those
shared objects would have to be repacked for both repositories. To fix this
waste of resources, we use object pools, which are essentially a repository
which holds the shared objects of both repositories.

The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.

## Lifetime of Object Pools

The lifetime of object pools is maintained via the
[ObjectPoolService](../proto/objectpool.proto), which provides various RPCs to
create and delete object pools as well as to add members to or remove members
from the pool.
```
