### Title
Missing Ownership/Fork-Network Validation in `LinkRepositoryToObjectPool` Allows Cross-Repository Object Pool Linkage - (File: internal/gitaly/service/objectpool/link.go)

### Summary
`LinkRepositoryToObjectPool` accepts an arbitrary `Repository` and an arbitrary `ObjectPool` in the same RPC request and links them together by writing the pool's path into the repository's `objects/info/alternates` file, without any check that the pool actually corresponds to the fork network/project that the repository belongs to. This mirrors the reported analog bug class ("no checks in place to verify a valid delegation/relationship before mutating shared state") applied to Gitaly's object-pool alternates mechanism instead of a Solidity boost-delegation mapping.

### Finding Description
The handler [1](#0-0)  only performs two checks before linking:
1. `s.locator.ValidateRepository(ctx, repository)` — confirms `repository` is a syntactically valid, existing Git repository.
2. `s.poolForRequest(ctx, req)` — resolves the `ObjectPool` via `objectpool.FromProto`, which only validates that the pool directory is a well-formed pool repository [2](#0-1) .

Neither check verifies that `repository` was actually forked from, or is otherwise entitled to be a member of, `pool`. The actual linking logic in `objectpool.Link` [3](#0-2)  unconditionally computes the relative path from the repository to the pool and, if not already linked, writes it into the repository's alternates file [4](#0-3) . Once linked, Git treats the pool's `objects` directory as part of the repository's own object search path — any object present in the pool becomes readable through the linked repository, and dangling/unreferenced objects retained by the pool (per its own housekeeping design, see `doc/object_pools.md`) can also leak into an unrelated repository's history.

The only other enforced constraint is infrastructural rather than authorization-based: the transaction middleware requires the pool and the repository to reside in the same storage/partition [5](#0-4) , which prevents cross-storage linking but says nothing about whether the two repositories have any legitimate relationship. As the service doc comment for `LinkRepositoryToObjectPool` itself notes, the RPC's only job is to add the alternates link — no ownership/fork-network validation is documented or implemented [6](#0-5) .

This is functionally the same defect pattern as the reported `updateUserBoost()` issue: a state-mutating entry point (`LinkRepositoryToObjectPool` / `updateUserBoost`) accepts caller-supplied identifiers for both sides of a relationship (`repository`+`pool` / `user`+`pool`) and mutates persistent linkage state (alternates file / `userBoost` mapping) without checking that the relationship was established through the proper flow (fork creation + `CreateObjectPool` / `delegateBoost`).

### Impact Explanation
If this RPC is reachable with a caller-controlled `object_pool`/`repository` pair (e.g. through insufficient upstream validation of the fields in the request, a compromised/relayed internal call, or any path where Rails' authorization does not itself enforce fork-network membership before forwarding to Gitaly), an attacker can:
- Force an arbitrary repository to become an alternate-object member of an unrelated object pool, exposing that pool's objects (including private/dangling ones) to anyone with read access to the victim repository — a cross-repository object disclosure.
- Corrupt the repository's dependency graph on that pool: subsequent `DeleteObjectPool` of the unrelated pool (which has "no safety checks" per its own doc comment [7](#0-6) ) would silently corrupt the victim repository, since Gitaly has no record that this repo depends on that pool.

### Likelihood Explanation
`LinkRepositoryToObjectPool` is a standard mutator RPC in `ObjectPoolService`, reachable from any gRPC client authorized to call Gitaly's repository/object-pool RPCs (normally used during fork creation flows). The request message lets the caller independently set both the `Repository` and `ObjectPool.Repository` fields [8](#0-7) ; nothing in the handler ties them to a common ancestry, so any caller able to reach this RPC with attacker-influenced repository identifiers can trigger the mismatch.

### Recommendation
Add an authorization/ownership check before linking: verify that `repository`'s recorded object pool relationship (e.g., via fork-network/project metadata tracked outside Gitaly, or a Gitaly-side record of which pool a repo was created against) matches the `ObjectPool` supplied in the request, rejecting the call otherwise. At minimum, restrict `LinkRepositoryToObjectPool` to only succeed if the target repository has no pre-existing alternate link to a *different* pool (already partially enforced in `linkedToRepository` [9](#0-8) ) and require that pool/repository membership be established exclusively through the `CreateFork`/`CreateObjectPool` flow, never through an independently callable `Link` with arbitrary pairs.

### Proof of Concept
1. Create repository `A` and derive `ObjectPool P_A` from it via `CreateObjectPool`.
2. Create an unrelated repository `B` (not forked from `A`).
3. Call `LinkRepositoryToObjectPool(Repository: B, ObjectPool: P_A)`. The handler only validates `B` exists and `P_A` is a well-formed pool directory [10](#0-9) ; it succeeds and writes `P_A`'s path into `B/objects/info/alternates`.
4. `B` now transparently exposes every object in `P_A` (including any objects retained there that are not reachable from `A`'s current refs) to anyone with read access to `B`, and a later `DeleteObjectPool(P_A)` will corrupt `B` despite `B` never having been legitimately forked from `A`.

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

**File:** internal/git/objectpool/link.go (L169-204)
```go
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

**File:** internal/gitaly/storage/storagemgr/middleware.go (L330-360)
```go
	}

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

**File:** proto/objectpool.proto (L100-107)
```text
// CreateObjectPoolRequest is a request for the CreateObjectPool RPC.
message CreateObjectPoolRequest {
  // object_pool is the object pool to create. This field controls where exactly the object pool will
  // be created.
  ObjectPool object_pool = 1 [(target_repository)=true];
  // origin is the repository from which the object pool shall be created.
  Repository origin = 2 [(additional_repository)=true];
}
```

**File:** proto/go/gitalypb/objectpool_grpc.pb.go (L62-64)
```go
	// DeleteObjectPool deletes the object pool. There are no safety checks in place, so if any
	// repository is still using this object pool it will become corrupted.
	DeleteObjectPool(ctx context.Context, in *DeleteObjectPoolRequest, opts ...grpc.CallOption) (*DeleteObjectPoolResponse, error)
```
