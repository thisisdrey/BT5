### Title
`LinkRepositoryToObjectPool` does not reject a request whose `repository` is the object pool itself, allowing self-referential alternates corruption - ([File: internal/gitaly/service/objectpool/link.go])

### Summary
The Sherlock report's root cause is that `matchOrder()` never checks whether the order's maker and taker are the same identity, letting a user satisfy both sides of a relationship that is supposed to involve two distinct parties and thereby dodge one leg of the fee. The Gitaly analog is `LinkRepositoryToObjectPool`, which never checks whether the caller-supplied `repository` is the same repository as `object_pool.repository`. Both fields are ordinary, unprivileged RPC inputs, and Gitaly proceeds to write an alternates file that makes a repository an alternate of itself.

### Finding Description
`LinkRepositoryToObjectPool` builds the pool from `req.GetObjectPool()` and the member repo from `req.GetRepository()` with no equality check between them: [1](#0-0) 

`poolForRequest` only validates that the pool repository itself is a valid pool directory — it does nothing to compare it against the member repository: [2](#0-1) 

`objectpool.Link` then computes the relative alternates path between `pool` and `repo` and, if not already linked, writes it into `repo`'s `objects/info/alternates` file: [3](#0-2) 

`getRelativeObjectPath` computes the alternates target purely from `pool.Path()` and `repo.Path()` with no distinctness guard: [4](#0-3) 

When `repository == object_pool.repository` (same storage + relative path), `poolPath == repoPath`, so `getRelativeObjectPath` computes `filepath.Rel(repoPath/objects, repoPath) + "/objects"`, which resolves to `"../objects"` relative to the repo's own `objects` directory — i.e., the repository's alternates file ends up pointing back at its own `objects` directory. This is a self-referential alternate, a state Git tooling (and Gitaly's own `linkedToRepository`/`stats.AlternatesInfoForRepository` logic) is not designed to handle safely, and `removeMemberBitmaps` will also run pool vs. member bitmap comparisons against the same directory. As with the Sherlock bug — where the code never rejects `maker == taker` — Gitaly never rejects `member == pool`, letting an ordinary caller push the two "distinct-party" RPC fields into an identical value and reach unintended, unvalidated code paths.

### Impact Explanation
A self-referential alternates entry can corrupt the repository's object resolution: subsequent Git operations (fsck, repack, clone) may loop, error out, or silently treat the repository's own objects directory as an "alternate," undermining the invariants that `internal/git/objectpool` relies on (e.g., `linkedToRepository`'s assumption that `pool` and `repo` are different repositories, and `removeMemberBitmaps` deleting bitmaps under the false pretense that pool and member are distinct). This can produce repository unavailability/DoS for that repository (a handler-triggered corruption), consistent with the "DoS of a handler" acceptance criterion.

### Likelihood Explanation
Both `repository` and `object_pool.repository` are attacker/client-controlled fields of a single authenticated but otherwise unprivileged gRPC request; no special server-side privilege is needed to set them equal, and existing test coverage in `link_test.go` never exercises the "member == pool" case, indicating the condition is untested and unguarded.

### Recommendation
In `LinkRepositoryToObjectPool` (or `poolForRequest`/`objectpool.Link`), explicitly compare `req.GetRepository()` against `req.GetObjectPool().GetRepository()` (storage name + relative path, resolved the same way `sameRepository`/`storage.RepoPathEqual` do elsewhere in the codebase, e.g. as in `internal/gitaly/service/repository/replicate.go`'s same-storage check) and reject the request with an invalid-argument error when they refer to the same repository, before any alternates file is written.

### Proof of Concept
1. Create a repository `R` and turn it into an object pool `P` whose `Repository` field equals `R`'s storage/relative path is not required — instead, call `CreateObjectPool` normally with origin `R` to get pool `P`.
2. Issue `LinkRepositoryToObjectPool{ Repository: P.Repository, ObjectPool: { Repository: P.Repository } }` — i.e., pass the pool's own repository as both the member and the pool.
3. `poolForRequest` resolves `pool` from `P.Repository`; `repo := s.localRepoFactory.Build(P.Repository)` builds an identical repo object; `pool.Link(ctx, repo)` is called with `pool.Repo == repo`.
4. `getRelativeObjectPath` computes a self-pointing relative path and `Link` writes it to `P`'s own `objects/info/alternates`, leaving the pool repository alternate-linked to itself with no error returned.

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

**File:** internal/git/objectpool/link.go (L149-166)
```go
func getRelativeObjectPath(ctx context.Context, pool, repo *localrepo.Repo) (string, error) {
	poolPath, err := pool.Path(ctx)
	if err != nil {
		return "", fmt.Errorf("getting object pool path: %w", err)
	}

	repoPath, err := repo.Path(ctx)
	if err != nil {
		return "", fmt.Errorf("getting repository path: %w", err)
	}

	relPath, err := filepath.Rel(filepath.Join(repoPath, "objects"), poolPath)
	if err != nil {
		return "", err
	}

	return filepath.Join(relPath, "objects"), nil
}
```
