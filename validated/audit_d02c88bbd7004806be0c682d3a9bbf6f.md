Found the analog. `repoutil.Create` (used by every repository-creating RPC, including `CreateRepository`, forks, and `CreateObjectPool`'s target-pool creation) performs an equality/existence check against a caller-supplied `RelativePath`, which is exactly the "external griefer can pre-populate shared state to break a postcondition" pattern from the Tempus finding.## Analog Found

### Title
Repository/pool creation permanently DoS'd via directory-path pre-creation griefing - (File: `internal/gitaly/repoutil/create.go`)

### Summary
`repoutil.Create`, the shared helper used by every repository-creating RPC (plain `CreateRepository`, fork creation, and `CreateObjectPool`), determines success or failure purely from an `os.Stat` existence check on the target relative path. Because the relative path is caller-supplied and the pre-creation of that directory is not scoped to the legitimate owner of the operation, an unprivileged caller that can reach any repository/pool-creating RPC can pre-create a directory at a path that another tenant's forthcoming repository or object pool will use, permanently blocking that legitimate creation with `AlreadyExists` — mirroring the Tempus `assert(balance == 0)` griefing pattern where an attacker pre-populates shared state to break a postcondition/precondition equality check.

### Finding Description
`Create()` performs two identical existence checks against `targetPath` (derived from the untrusted `storage.Repository{StorageName, RelativePath}` input) and fails hard with `structerr.NewAlreadyExists("repository exists already")` whenever anything already occupies that path: [1](#0-0) 

and again after acquiring the repository lock: [2](#0-1) 

This is functionally the same invariant-check pattern as the Tempus `assert(tempusAMM.balanceOf(address(this)) == 0)`: a postcondition ("this path must be empty/exclusively mine") that is evaluated against externally-writable shared state rather than state the caller provably controls. Just as an attacker can send a dust amount of LP tokens to the Tempus controller to make `balanceOf == 0` false forever, an attacker who can invoke any Gitaly repository-creation RPC with an attacker-chosen `RelativePath` can create a directory (or even an empty file) at a path that a legitimate, not-yet-created repository or object pool will later try to occupy. Once occupied, every legitimate call to `Create()` for that path fails with `AlreadyExists` both at the pre-lock and post-lock stat checks, and there is no mechanism in `Create()` to recover or distinguish "pre-existing garbage" from "real repository."

This helper is reused for object pool creation as well: `CreateObjectPool` calls `repoutil.Create` directly on the pool's target repository: [3](#0-2) 

Because GitLab's hashed-storage layout derives relative paths deterministically (e.g., from project/pool identifiers that are frequently enumerable or predictable from public API responses), an attacker with legitimate but unprivileged access to the Gitaly RPC surface (e.g., their own repository/import/fork requests) can compute the future relative path of a target project or its pool and race to occupy it before the victim's creation flow runs.

### Impact Explanation
Any repository, fork, or object-pool creation whose target relative path can be predicted or raced by another tenant can be permanently prevented from being created — a persistent denial of service requiring manual administrator cleanup of the squatted directory, exactly matching the "becomes unusable" impact rated Medium in the source finding. This affects `CreateRepository`, `CreateFork`, `CreateObjectPool`, and any other RPC that funnels through `repoutil.Create`.

### Likelihood Explanation
Likelihood depends on (a) whether an attacker's Gitaly-facing caller can supply/influence a `RelativePath` corresponding to another tenant's future repository or pool, and (b) whether the relative path is predictable/enumerable (e.g., derived from sequential/guessable project or pool identifiers) ahead of the real creation call. I was not able to fully confirm within Gitaly's own codebase how the hashed-storage relative path is generated (that logic lives in GitLab Rails/Workhorse, outside this repo), so the exact attacker precondition — how a low-privilege user obtains or predicts a victim path and gets Gitaly to accept an out-of-scope `RelativePath` for a creation call — could not be verified end-to-end from the Gitaly source alone.

### Recommendation
Do not treat "path already exists" as an unconditional, permanent failure. `Create()` should distinguish between a real, previously-committed repository (tracked via the repository counter / transaction KV store) and stray filesystem entries, and should be able to reclaim/clean up non-repository directories left at the target path (similar to how `config.SetupRuntimeDirectory` removes stale directories it can prove are orphaned) before failing the caller. Additionally, callers of `repoutil.Create` should validate that `RelativePath` values are scoped/authorized for the invoking tenant rather than accepting an arbitrary caller-chosen path at face value.

### Proof of Concept
1. Attacker determines (or brute-forces) the relative storage path that Gitaly will use for a victim's upcoming repository or object pool creation.
2. Attacker issues any Gitaly RPC that reaches `repoutil.Create` (e.g., `CreateRepository`) with that exact `RelativePath`, creating a directory there ahead of the victim.
3. When the victim's legitimate creation flow later calls `repoutil.Create` with the same `RelativePath`, `internal/gitaly/repoutil/create.go:98-101` (or the post-lock recheck at `internal/gitaly/repoutil/create.go:202-205`) finds the path occupied and returns `structerr.NewAlreadyExists("repository exists already")`, permanently failing the victim's operation until an administrator manually removes the squatted directory.

### Citations

**File:** internal/gitaly/repoutil/create.go (L96-104)
```go
	// The repository must not exist on disk already, or otherwise we won't be able to
	// create it with atomic semantics.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("pre-lock stat: %w", err)
	}
```

**File:** internal/gitaly/repoutil/create.go (L197-208)
```go
	// Now that the repository is locked, we must assert that it _still_ doesn't exist.
	// Otherwise, it could have happened that a concurrent RPC call created it while we created
	// and seeded our temporary repository. While we would notice this at the point of moving
	// the repository into place, we want to be as sure as possible that the action will succeed
	// previous to the first transactional vote.
	if _, err := os.Stat(targetPath); !errors.Is(err, fs.ErrNotExist) {
		if err == nil {
			return structerr.NewAlreadyExists("repository exists already")
		}

		return fmt.Errorf("post-lock stat: %w", err)
	}
```

**File:** internal/gitaly/service/objectpool/create.go (L41-61)
```go
	if err := repoutil.Create(ctxWithoutTransaction, s.logger, s.locator, s.gitCmdFactory, s.catfileCache, s.txManager, s.repositoryCounter, poolRepo, func(poolRepo *gitalypb.Repository) error {
		if _, err := objectpool.Create(
			ctxWithoutTransaction,
			s.logger,
			s.locator,
			s.gitCmdFactory,
			s.catfileCache,
			s.txManager,
			s.housekeepingManager,
			&gitalypb.ObjectPool{
				Repository: poolRepo,
			},
			s.localRepoFactory.Build(in.GetOrigin()),
		); err != nil {
			return err
		}

		return nil
	}, repoutil.WithSkipInit()); err != nil {
		return nil, structerr.New("creating object pool: %w", err)
	}
```
