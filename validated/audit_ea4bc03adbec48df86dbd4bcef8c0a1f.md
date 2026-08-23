[1](#0-0) 

### Title
Unauthorized cross-repository object exposure via `LinkRepositoryToObjectPool` lacking ownership/provenance validation - (File: `internal/gitaly/service/objectpool/link.go`, `internal/git/objectpool/link.go`)

### Summary
The JBToken report describes a privileged operation (`mint`) that is reachable and unrestricted once ownership is transferred to an untrusted party via `changeTokenOf`, letting that party mint unlimited tokens because `mint` only checks `onlyOwner` and never validates the caller's relationship to the token/project it's minting for. The structural analog in Gitaly is `ObjectPoolService.LinkRepositoryToObjectPool`: the RPC links any caller-specified `repository` to any caller-specified `object_pool` and never verifies that the two repositories have any prior relationship (e.g., that the pool was actually created from that repository's fork lineage), analogous to a privileged action being performed without validating the caller's actual entitlement to the target resource.

### Finding Description
`LinkRepositoryToObjectPoolRequest` accepts two independent repository references — `object_pool` (marked `additional_repository`) and `repository` (marked `target_repository`) — with no cross-check that the `repository` is actually a fork/origin member of that specific `object_pool`. [2](#0-1) 

The handler only validates that the `repository` field is well-formed and that the `object_pool` resolves to a valid pool directory (`poolForRequest` → `objectpool.FromProto`), then immediately calls `pool.Link(ctx, repo)`. [3](#0-2) 

The core `Link` function in `internal/git/objectpool/link.go` writes the pool's path into the target repository's `objects/info/alternates` file whenever the repository isn't already linked to a *different* pool — again with no check that the caller is authorized to associate these two specific repositories. [4](#0-3) 

The only safety check present (`linkedToRepository`) merely detects whether the repository is already linked to *some* pool and rejects linking it to a *second, different* one — it does nothing to verify that the pool being linked is legitimately derived from, or otherwise entitled to serve, the target repository. [5](#0-4) 

The pool validity check (`storage.IsPoolRepository`) used during pool creation only verifies the pool's relative path follows the pool directory naming convention (a hashed `@pools/...` structure) — it is not a provenance or ownership check. [6](#0-5) 

As documented, once linked, "objects contained in the object pool will be deduplicated for this repository," meaning all of the pool's objects (potentially originating from a completely unrelated, private repository) become transparently readable through the linked repository via the alternates mechanism. [7](#0-6) 

### Impact Explanation
Just as the JBToken owner-transfer bug let an untrusted party mint arbitrary tokens with no restriction on amount, an unrestricted `LinkRepositoryToObjectPool` call lets a caller connect an arbitrary repository to an arbitrary object pool, exposing every Git object stored in that pool (private commits, blobs, trees from another tenant's repository) to readers of the linked repository. This is a cross-repository object disclosure / storage-isolation escape, matching the "object-pool and alternates isolation" and "cross-repository object access" categories explicitly in scope.

### Likelihood Explanation
Gitaly's own documentation states this RPC set has "no safety checks in place" for related `DeleteObjectPool`, indicating pool safety is expected to be enforced entirely by the calling layer (Rails/GitLab), not by Gitaly itself. [8](#0-7) 
Any client with direct or indirect gRPC access to `ObjectPoolService` (e.g., a compromised/lower-privileged internal caller, or any component that can construct a `LinkRepositoryToObjectPoolRequest` with attacker-influenced repository/pool paths) can trigger this without additional exploitation steps, since Gitaly performs no server-side relationship check.

### Recommendation
Add provenance validation in `LinkRepositoryToObjectPool` (and ideally `Link` in `internal/git/objectpool/link.go`) to confirm the target `repository` is an authorized/expected member of the specific `object_pool` (e.g., via a persisted pool-membership record or a signed/verifiable link established only at `CreateObjectPool` time), rather than trusting caller-supplied path pairs at link time.

### Proof of Concept
1. Attacker-controlled or otherwise unprivileged caller creates (or already knows the relative path of) an object pool `P` seeded from victim's private repository `R_victim` via `CreateObjectPool`.
2. Attacker calls `LinkRepositoryToObjectPool` with `object_pool = P` and `repository = R_attacker` (a repository the attacker fully controls/can read).
3. `Link` succeeds because `linkedToRepository` only rejects the case where `R_attacker` is already linked to a *different* pool; it never checks whether `P` legitimately belongs to `R_attacker`'s lineage. [9](#0-8) 
4. `R_attacker`'s `objects/info/alternates` now points at `P`'s object directory, and all of `R_victim`'s objects previously pooled into `P` become readable through `R_attacker` (e.g., via `CommitService`/`ReadObject`-style RPCs against `R_attacker`), as demonstrated functionally (for the legitimate-pool case) in [10](#0-9) .

### Citations

**File:** proto/objectpool.proto (L44-46)
```text
  // DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any
  // repository is still using this object pool it will become corrupted.
  rpc DeleteObjectPool(DeleteObjectPoolRequest) returns (DeleteObjectPoolResponse) {
```

**File:** proto/objectpool.proto (L52-58)
```text
  // LinkRepositoryToObjectPool links the specified repository to the object pool. Objects contained
  // in the object pool will be deduplicated for this repository when repacking objects.
  rpc LinkRepositoryToObjectPool(LinkRepositoryToObjectPoolRequest) returns (LinkRepositoryToObjectPoolResponse) {
    option (op_type) = {
      op: MUTATOR
    };
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

**File:** internal/gitaly/service/objectpool/create.go (L22-29)
```go
	poolRepo := in.GetObjectPool().GetRepository()
	if poolRepo == nil {
		return nil, errMissingPool
	}

	if !storage.IsPoolRepository(poolRepo) {
		return nil, errInvalidPoolDir
	}
```

**File:** internal/gitaly/service/objectpool/link_test.go (L133-157)
```go
		{
			desc: "successful",
			request: &gitalypb.LinkRepositoryToObjectPoolRequest{
				Repository: repo,
				ObjectPool: poolProto,
			},
		},
	} {
		t.Run(tc.desc, func(t *testing.T) {
			_, err := client.LinkRepositoryToObjectPool(ctx, tc.request)
			testhelper.RequireGrpcError(t, tc.expectedErr, err)
			if tc.expectedErr != nil {
				return
			}

			// verify that the commit is present in the pool
			commitClient := gitalypb.NewCommitServiceClient(gittest.DialService(t, ctx, cfg))
			gittest.RequireObjectExistsAPI(t, ctx, commitClient, repo, poolCommitID)

			commit, err := localRepo.ReadCommit(ctx, git.Revision(poolCommitID))
			require.NoError(t, err)
			require.NotNil(t, commit)
			require.Equal(t, poolCommitID.String(), commit.GetId())
		})
	}
```
