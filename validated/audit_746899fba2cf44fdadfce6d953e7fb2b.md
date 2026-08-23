This confirms the vulnerability pattern. `LinkRepositoryToObjectPool` (`internal/gitaly/service/objectpool/link.go`) only checks: (1) that `repository` is a valid Git repo via `s.locator.ValidateRepository`, and (2) that `object_pool` is *shaped* like a pool via `storage.IsPoolRepository`/`objectpool.FromProto`'s `IsValid` check [1](#0-0) . Neither check verifies that the `object_pool` and `repository` are actually related (e.g., same fork network / same project lineage) — exactly analogous to the Astaria bug where `vaults[address(vault)] != address(0)` only checks existence, not whether the vault is the intended `PublicVault` type. Here, `objectpool.FromProto` and `IsValid` only confirm the target path *looks like* a pool and *is a valid git repo* [2](#0-1) , with no ownership/relationship check between `origin`/`repository` and the `object_pool`.

Since `Link` writes the pool's path into the repository's `objects/info/alternates` file [3](#0-2) , once linked, all objects in the pool become readable to the linked repo through Git's alternates mechanism — this is a cross-repository object disclosure path if an untrusted caller can supply an arbitrary (existing) `object_pool` path unrelated to their own repository.

### Title
Insufficient authorization allows linking arbitrary repository to unrelated ObjectPool, exposing pool's private objects - (File: internal/gitaly/service/objectpool/link.go)

### Summary
`LinkRepositoryToObjectPool` validates that the `repository` (target) is a legitimate Git repository and that the `object_pool` (additional repository) is *shaped* like a valid pool, but never verifies that the two are actually related (i.e., the caller's `repository` belongs to the same fork network / origin lineage as the `object_pool`). This mirrors the Astaria `lendToVault` bug class: a mapping/lookup only confirms the target *exists and is of the generic resource type*, not that it is the *specific* instance the caller is authorized to interact with.

### Finding Description
`LinkRepositoryToObjectPool` in [1](#0-0)  performs two checks:
1. `s.locator.ValidateRepository(ctx, repository)` — confirms `repository` exists and is valid.
2. `s.poolForRequest(ctx, req)` → `objectpool.FromProto` — confirms `object_pool` path matches the pool naming convention (`storage.IsPoolRepository`) and is a valid git repo (`pool.IsValid`), shown in [2](#0-1) .

Neither check establishes that `repository` is a legitimate member of `object_pool`'s fork network. `Link` then unconditionally writes the pool's object directory into `repository`'s `objects/info/alternates` file [4](#0-3) , making every object contained in the target pool implicitly readable by any process/user with read access to `repository` (via `git cat-file`, `git log`, clone, etc., since alternates are transparently consulted by Git). `FetchIntoObjectPool` similarly only checks that `origin` and `object_pool` share the same storage name [5](#0-4) , again without verifying project/fork-network relationship.

Because Gitaly itself has no concept of users/projects/permissions (that context is carried by the caller, e.g., GitLab Rails), the RPC-level lack of relational verification means any caller capable of invoking these mutator RPCs against a `repository` it controls can supply the relative path of an **arbitrary existing pool** it does not own, as long as it matches the pool naming pattern and resides on the same storage.

### Impact Explanation
An attacker able to trigger `LinkRepositoryToObjectPool` (or `FetchIntoObjectPool`) with a `repository` they control and an `object_pool` path belonging to an unrelated (possibly private) fork network can cause their repository's alternates file to point at that pool's object directory. This grants read access to every object in the victim pool — including objects from private repositories that were never intended to be shared — via ordinary Git read operations against the attacker's own repository. This is a concrete cross-repository object disclosure.

### Likelihood Explanation
Exploitability depends on whether the calling layer (e.g., GitLab Rails) independently verifies that the specific `object_pool` path supplied matches the one legitimately associated with the target `repository`'s fork network before invoking these Gitaly RPCs. Within Gitaly's own trust boundary, there is no such verification, so likelihood is high for any RPC caller/service that can supply attacker-influenced repository or pool paths, or under Praefect-mediated access where the two fields are independently controllable request parameters.

### Recommendation
Add an explicit relational check inside `LinkRepositoryToObjectPool` (and `FetchIntoObjectPool`) confirming that the `object_pool` is the one actually registered/associated with `repository`'s fork network (e.g., cross-referencing against the object-pool state manager's `ListPoolMembers`/pool-membership records shown in [6](#0-5) ) rather than relying solely on path-shape validation (`storage.IsPoolRepository`) and generic git-directory validity (`ValidateRepository`/`IsValid`).

### Proof of Concept
1. Attacker creates/owns `repo-a` on a shared Gitaly storage.
2. Attacker learns or guesses the relative path of a `@pools/xx/yy/<hash>.git` object pool belonging to an unrelated private fork network (pool paths are derived from source project's hashed path, potentially enumerable/predictable).
3. Attacker (or a compromised intermediate) issues `LinkRepositoryToObjectPool{ Repository: repo-a, ObjectPool: { Repository: { StorageName: "default", RelativePath: "<victim-pool>.git" } } }`.
4. `objectpool.FromProto` passes because `storage.IsPoolRepository` and `ValidateRepository` only check shape/existence, not ownership [7](#0-6) .
5. `Link` writes the victim pool's `objects` path into `repo-a`'s `objects/info/alternates` [8](#0-7) .
6. Attacker now runs `git cat-file`/`git log`/clone against `repo-a` and can read any object present in the victim's pool.

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

**File:** internal/git/objectpool/pool.go (L46-91)
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

	pool := &ObjectPool{
		Repo:                localrepo.New(logger, locator, gitCmdFactory, catfileCache, proto.GetRepository()),
		logger:              logger,
		locator:             locator,
		gitCmdFactory:       gitCmdFactory,
		txManager:           txManager,
		housekeepingManager: housekeepingManager,
	}

	if !pool.IsValid(ctx) {
		return nil, ErrInvalidPoolRepository
	}

	return pool, nil
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

**File:** internal/gitaly/service/objectpool/fetch_into_object_pool.go (L102-117)
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
```

**File:** internal/gitaly/storage/relational/state_manager_test.go (L74-89)
```go
func TestObjectPoolStateManager_LinkRepository(t *testing.T) {
	store := newTestStore(t)
	mgr := newTestManager(t, store)

	ctx := context.Background()

	err := mgr.NotifyCreatePool(ctx, "/path/to/pool1.git", "default", "upstream.git")
	require.NoError(t, err)

	err = mgr.NotifyLinkRepository(ctx, "/path/to/pool1.git", "member1.git")
	require.NoError(t, err)

	members, err := store.ListPoolMembers(ctx, "/path/to/pool1.git")
	require.NoError(t, err)
	require.Contains(t, members, "member1.git")
}
```
