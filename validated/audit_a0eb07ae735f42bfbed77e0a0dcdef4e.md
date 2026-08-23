### Title
`LinkRepositoryToObjectPool` allows linking any repository to an attacker-supplied object pool, enabling cross-repository object smuggling / disclosure - (File: `internal/gitaly/service/objectpool/link.go`)

### Summary
The reported ITS-hub bug allowed an attacker to make an "untracked" balance appear trustworthy simply by pointing a legitimate chain's accounting at an attacker-controlled token deployment, because the hub never verified that the token/manager actually originated from the token's real home chain. The structural flaw — a privileged, cross-domain "linking" operation that trusts a caller-supplied foreign identifier without validating its provenance relative to the target — has a concrete analog in Gitaly's `ObjectPoolService.LinkRepositoryToObjectPool` RPC.

### Finding Description
`LinkRepositoryToObjectPool` takes a `repository` (target) and an `object_pool` (additional repository) directly from the request and links them by writing the pool's path into the target repository's `objects/info/alternates` file: [1](#0-0) 

The handler only validates that the *target* repository is a well-formed, existing repository via `s.locator.ValidateRepository`, and that the *pool* repository is a validly-formed pool via `poolForRequest`/`objectpool.FromProto`: [2](#0-1) 

There is no check that the supplied `object_pool` was actually created from, or is otherwise associated with, the `repository` being linked (e.g. via `CreateFork`/`CreateObjectPool` ancestry). Any caller able to invoke this RPC for a repository they can reach can supply the relative path of *any* other repository configured as a pool on the same storage, and `git/objectpool.Link` will write it into the target's alternates file as long as it resolves within the storage root and isn't already linked to a different pool: [3](#0-2) 

Once linked, Git's alternate-object-directory search path mechanism causes the target repository to transparently "see" every object contained in the foreign pool — this is the same alternates search-path mechanism documented for quarantine and object-pool object resolution: [4](#0-3) [5](#0-4) 

This mirrors the report's root cause exactly: a component trusts a caller-supplied cross-domain reference (a token/manager address in the report, an object-pool repository path here) as if it were verified to originate from the correct owning context, without recording/validating the "origin" relationship.

### Impact Explanation
Linking an unrelated repository to an attacker-controlled or unrelated object pool allows:
- **Cross-repository object disclosure**: objects placed in the foreign pool (e.g., a private repository's pool, or a pool the attacker fully controls) become resolvable from the target repository, and vice versa — any object the attacker has pushed into their own pool becomes visible/fetchable through the linked repository once merged. This is a direct instance of the "cross-repository object access" class called out as acceptable analog scope.
- **Storage/tracking corruption analogous to the report**: an attacker can make objects that logically belong to one context appear as native, dependency-satisfying content of another repository — the same "untracked balance moved to a controlled context and then merged into a trusted context" pattern from the ITS report, since after `Link`, subsequent fetch/repack/GC operations may consider the foreign pool's objects part of the linked repository's reachable set, and `DisconnectGitAlternates`/`FetchIntoObjectPool` can further hard-link those objects permanently into the target's own `objects/` directory: [6](#0-5) 
- Because `LinkRepositoryToObjectPool` is a `MUTATOR` RPC reachable from ordinary write-path flows (fork/object-pool management), an unprivileged actor able to invoke it against a repository they control, pointed at a pool they also control (or a pool belonging to another tenant they can merely name), can use it to inject foreign object graphs into a target repository's alternate search path without any relationship check.

### Likelihood Explanation
The RPC is a normal, documented part of the ObjectPoolService workflow used during forking, and its request schema places no restriction requiring the pool to have been created from the target repository. Any client capable of calling `LinkRepositoryToObjectPool` for a repository it manages, while specifying an arbitrary existing pool path, can trigger the described behavior — this only requires knowledge of a valid pool relative path (which is often predictable, e.g. tied to `@hashed/...` layout in `@pools` directories) and the ability to invoke a standard, exposed RPC. No malicious peer, MITM, or leaked-token scenario is required.

### Recommendation
- Record and enforce object-pool "origin" provenance: when `CreateObjectPool` creates a pool from a given `origin` repository, persist that association (as Gitaly's partitioning code already does with `getAlternatePartitionID`/`ErrAlternateHasAlternate` checks for cyclic alternates), and have `LinkRepositoryToObjectPool` reject links where the pool's recorded lineage has no legitimate relationship to the requested `repository` (e.g., not itself, not a fork descendant, or not previously authorized).
- Alternatively/additionally, require authorization checks at the higher (Rails/Workhorse) layer to ensure the caller has write access to both the target repository and the specific object pool before invoking the RPC, rather than trusting the identifiers embedded in the request alone.
- Add regression tests asserting that `LinkRepositoryToObjectPool` fails when the object pool was not derived from (or previously associated with) the target repository.

### Proof of Concept
1. Attacker creates `repoA` and calls `CreateObjectPool` with `origin = repoA`, producing `poolA` fully populated with attacker-chosen objects (including secret-looking blobs/commits they construct).
2. Attacker creates an unrelated `repoB` (or targets any repository they have write access to on the same storage) and calls:
   ```
   LinkRepositoryToObjectPool(repository = repoB, object_pool = poolA)
   ```
3. `link.go`'s handler validates only that `repoB` exists and that `poolA` is a well-formed pool repository — it performs no check that `poolA` originated from or is otherwise tied to `repoB`: [1](#0-0) 
4. `objectpool.Link` writes `poolA`'s relative object directory into `repoB/objects/info/alternates`: [7](#0-6) 
5. From this point, any Git operation on `repoB` (fetch, cat-file, fsck, repack) resolves objects through `poolA`'s object directory per the alternates search-path mechanism, and `FetchIntoObjectPool`/`DisconnectGitAlternates` can further merge those objects permanently into `repoB`, achieving cross-repository object smuggling analogous to the reported untracked-balance bypass.

*Note:* I was unable to locate `internal/git/objectpool/objectpool.go` (the file defining `FromProto`/pool validation) in the indexed codebase, so I could not fully confirm whether `objectpool.FromProto` performs any hidden provenance check beyond "is this a valid pool repository." If a background Devin session is started, it should read that file directly to confirm there is no origin-relationship enforcement before finalizing remediation.

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

**File:** internal/git/objectpool/link.go (L19-66)
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

**File:** internal/git/dirs.go (L21-43)
```go
// ObjectDirectories looks for Git object directories, including
// alternates specified in objects/info/alternates.
//
// CAVEAT Git supports quoted strings in here, but we do not. We should
// never need those on a Gitaly server.
func ObjectDirectories(ctx context.Context, logger log.Logger, storageRoot, repoPath string) ([]string, error) {
	objDir := filepath.Join(repoPath, "objects")
	return altObjectDirs(ctx, logger, storageRoot+string(os.PathSeparator), objDir, 0)
}

// AlternateObjectDirectories reads the alternates file of the repository and returns absolute paths
// to its alternate object directories, if any. The returned directories are verified to exist and that
// they are within the storage root. The alternate directories are returned recursively, not only the
// immediate alternates.
func AlternateObjectDirectories(ctx context.Context, logger log.Logger, storageRoot, repoPath string) ([]string, error) {
	dirs, err := ObjectDirectories(ctx, logger, storageRoot, repoPath)
	if err != nil {
		return nil, err
	}

	// first directory is the repository's own object dir
	return dirs[1:], nil
}
```

**File:** doc/object_pools.md (L10-12)
```markdown
The sharing of objects for a given repository and its object pool is done via
alternate object directories which Gitaly sets up when linking a repository to
an object pool by writing the `objects/info/alternates` file.
```

**File:** internal/git/objectpool/disconnect.go (L89-117)
```go
	objectFiles, err := findObjectFiles(altObjectDir)
	if err != nil {
		return err
	}

	repositoryRelativePath, err := filepath.Rel(f.Root(), repoPath)
	if err != nil {
		return fmt.Errorf("repository relative path: %w", err)
	}

	for _, path := range objectFiles {
		sourceRelativePath, err := filepath.Rel(f.Root(), filepath.Join(altObjectDir, path))
		if err != nil {
			return fmt.Errorf("source relative path: %w", err)
		}
		targetRelativePath := filepath.Join(repositoryRelativePath, "objects", path)

		if err := storage.MkdirAll(f, filepath.Dir(targetRelativePath)); err != nil {
			return err
		}

		if err := storage.Link(f, sourceRelativePath, targetRelativePath); err != nil {
			if errors.Is(err, fs.ErrExist) {
				continue
			}

			return err
		}
	}
```
