### Title
Any user who can call `LinkRepositoryToObjectPool` can link their own repository to an arbitrary, unrelated object pool and gain unlimited read access to its objects - ([File: internal/gitaly/service/objectpool/link.go])

### Summary
`LinkRepositoryToObjectPool` links a caller-supplied `Repository` to a caller-supplied `ObjectPool`, with no verification in Gitaly that the two repositories are related (e.g. that the pool was actually created from, or is otherwise authorized for, the target repository). This mirrors the `VouchFaucet.claimVouch()` bug class: a function that grants an unbounded/unauthorized resource (trust / object access) to the caller because the *only* enforcement point is external (an "approved caller" convention) rather than a validated relationship in the code path itself.

### Finding Description
`LinkRepositoryToObjectPool` accepts two independently-controlled repository references: [1](#0-0) 

- `object_pool` is tagged `additional_repository`
- `repository` is tagged `target_repository`

The RPC handler validates only that `repository` is a well-formed repository and that a pool can be resolved from `object_pool`; it never checks that the object pool was created from, or otherwise belongs to, the same logical owner/project as `repository`: [2](#0-1) 

`poolForRequest` simply resolves whatever `ObjectPool` proto message the caller supplied into an `objectpool.ObjectPool` object, with no ownership or ACL check: [3](#0-2) 

The underlying `Link` function then unconditionally writes an `objects/info/alternates` file in `repository` pointing at `pool`'s object directory: [4](#0-3) 

Once linked, all objects in the pool repository become fully readable from the target repository via any object-access RPC (e.g. `CommitService`, `GetArchive`, `GetBlob`), since Git treats alternates as part of the object database. The docstring for the RPC even states "Objects contained in the object pool will be deduplicated for this repository," implying the object pool is meant to be linked only by legitimate members/forks of the same origin repository - but nothing in the Gitaly code enforces that assumption. Enforcement is left entirely to the calling layer (GitLab Rails access checks and its notion of "the pool belongs to this project"), analogous to `VouchFaucet.claimVouch()` relying purely on an external assumption ("only approved users will call this") instead of validating anything on-chain/in-code.

### Impact Explanation
Any actor capable of invoking `LinkRepositoryToObjectPool` for a repository they control (e.g. a Gitaly client with valid auth token issued to any project it has write access to) can point that repository's alternates at an arbitrary pool repository whose relative path they can discover or guess (e.g. `@pools/<hash>.git` naming schemes are predictable), thereby gaining read access to all objects contained in that pool - including objects from private/unrelated repositories that share the pool. This is a cross-repository object disclosure, directly matching the "cross-repository object access" class the task explicitly calls out. It also lets an attacker corrupt or pollute their own alternates state that other RPCs (`OptimizeRepository`, `DisconnectGitAlternates`, `RepositoryInfo`) assume is well-formed and derived from a legitimate pool relationship, potentially producing further consistency issues (e.g. `DisconnectGitAlternates`'s claim that unlinking is safe "for the primary object pool member" no longer holds once arbitrary members can be linked).

### Likelihood Explanation
Likelihood is high for any deployment where Gitaly's gRPC endpoint is reachable with a valid repository-scoped auth token but where the higher-level authorization layer (GitLab Rails) does not perfectly restrict which pool a given repository is permitted to link to, or in any direct-Gitaly-access scenario (e.g. custom RPC clients, internal tooling, or a compromised/lower-privileged service token) where the Rails-level check is bypassed entirely. Because Gitaly performs no relationship validation itself, the security boundary is fully outsourced to a caller that this RPC's own proto/service code does not control.

### Recommendation
In `LinkRepositoryToObjectPool` (and in `objectpool.ObjectPool` resolution), verify that the object pool is legitimately associated with the target repository before writing alternates - e.g., by checking a persisted pool-to-origin relationship (already tracked via `objectPoolStateManager`/`relational.ObjectPoolStateManager` in the object pool server) rather than trusting the caller-supplied `ObjectPool` message unconditionally. At minimum, require that the object pool and target repository reside in storages/partitions under a common, previously-established ownership record created via `CreateObjectPool`, and reject links to pools with no recorded relationship to the target repository.

### Proof of Concept
1. Attacker has (or obtains) a valid Gitaly auth token scoped to `repoA` (their own repository).
2. Attacker discovers or brute-forces the relative path of an unrelated private pool repository `poolB` (created from victim's `repoB`).
3. Attacker calls:
```
LinkRepositoryToObjectPool(
  Repository:  repoA,
  ObjectPool:  { Repository: poolB },
)
```
4. `internal/gitaly/service/objectpool/link.go` resolves `poolB` via `poolForRequest` with no ownership check and calls `pool.Link(ctx, repoA)`.
5. `internal/git/objectpool/link.go` writes `repoA/objects/info/alternates` pointing at `poolB`'s object directory.
6. Attacker can now fetch/read any object in `poolB` (and thus objects originally private to `repoB`) through `repoA` via standard object-access RPCs.

### Citations

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
