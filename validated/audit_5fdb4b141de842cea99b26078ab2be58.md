### Title
Object pool path squatting via predictable pool relative path permanently blocks `CreateObjectPool` (analogous to UUID pre-emption DoS) - (File: `internal/gitaly/service/objectpool/create.go`)

### Summary
`CreateObjectPool` and the underlying `repoutil.Create`/`objectpool.Create` helpers treat "target path already exists" as a terminal, non-recoverable error. Because Rails-managed object pool relative paths follow a fixed, deterministic naming scheme (`@pools/<xx>/<yy>/<sha256>.git`) validated purely by regex in `IsRailsPoolRepository`, and because the on-disk existence check has no notion of "who is entitled to this path" (unlike a signed/authorized reservation), any actor able to trigger repository creation at an arbitrary relative path within the storage can pre-occupy the exact path that a legitimate future `CreateObjectPool` call for a given project will target. This mirrors the Optimism bug class: the "correct" claim (the legitimate object pool) can never be created because the identifier/path is deterministic and was squatted before the honest party's use of it, and the check is a simple existence/uniqueness gate with no tie-breaking or override mechanism.

### Finding Description
`storage.IsPoolRepository`/`IsRailsPoolRepository` only validate that a relative path matches the shape `@pools/xx/yy/<64-hex>.git`; they do not verify that the caller is authorized to use that specific path, nor bind the path to any secret or unpredictable value at the Gitaly layer: [1](#0-0) 

`CreateObjectPool` only checks the shape of the pool path, then delegates to `repoutil.Create`, whose only protection against a colliding path is a pre-existence `os.Stat` check that returns a terminal `AlreadyExists`/`FailedPrecondition` error with no retry or override path: [2](#0-1) [3](#0-2) 

The lower-level `objectpool.Create` performs the same kind of check on the exact target path before it clones from the source: [4](#0-3) 

Because the pool path is a pure function of externally-supplied data (the pool's hash-based relative path, or in the Praefect case, `DerivePoolPath(repositoryID)` which is a simple SHA256 of a small, monotonically increasing, unauthenticated integer sequence), an actor who can create a repository at an arbitrary relative path (e.g., via any RPC that lets a client fully specify `RelativePath`, or by consuming the Praefect-wide repository ID sequence with cheap repository-creation calls) can predict and pre-create the exact path a legitimate pool will later need: [5](#0-4) [6](#0-5) 

Once occupied, every subsequent legitimate `CreateObjectPool` attempt for that project fails permanently with `FailedPrecondition("target path exists already")` / `AlreadyExists("repository exists already")`, exactly as the report's "GameAlreadyExists" revert permanently blocks the honest claim once the malicious one claims the UUID first.

### Impact Explanation
This blocks object pool creation (deduplication) for the targeted repository network indefinitely: every fork of the victim project will fail to be linked to a pool or will never get a pool created, forcing full, non-deduplicated object storage for every fork forever, and consuming Gitaly disk resources per this DoS pattern. This is a resource-limit/DoS impact on a repository-creation handler, consistent with the categories accepted by the validation rules (DoS of a handler via storage-path collision).

### Likelihood Explanation
Likelihood is bounded by the reachability of an RPC where the caller (rather than the trusted server) controls the exact relative path used for an object pool, or by the ability to churn the global Praefect repository-ID sequence to predict a colliding derived path. Both preconditions require some legitimate repository-creation capability but not privileged Gitaly access — matching the "ordinary user ... crafted RPC field" scope. The main uncertainty (which would need confirmation against how GitLab Rails computes and submits pool relative paths) is whether a normal user-facing action can supply an attacker-influenced `RelativePath` value to `CreateObjectPool`/`CreateRepository` matching a not-yet-created pool's future path; if so, the squat is fully deterministic and cheap.

### Recommendation
- Do not allow arbitrary `RelativePath`/pool path values to be provided directly to `CreateObjectPool`/`CreateRepository`; derive and validate the exact target path from a value that only the trusted caller (Rails/Praefect) controls and that cannot be pre-computed and squatted by an untrusted party (e.g., bind the path to a value only known/settable after the source repository/pool relationship is authoritatively decided).
- When `CreateObjectPool` finds an existing but mismatched/foreign repository at the target pool path, treat it as invalid/reclaimable rather than a terminal `AlreadyExists`, or provide an administrative path to detect and clear squatted pool directories (similar to `ScanPoolMetadata`'s broken-pool detection) automatically rather than leaving the state permanently blocked.
- Consider validating that the SHA256 component of `@pools/xx/yy/<sha256>.git` actually corresponds to a value that can only be derived from privileged, unpredictable input.

### Proof of Concept
1. Determine (or predict) the deterministic future object pool relative path for a target project, e.g., by computing `@pools/<xx>/<yy>/<sha256(project_id)>.git` per `railsPoolDirRegexp` in `internal/gitaly/storage/repository_path.go`, or by observing `ReserveRepositoryID`'s Praefect-wide sequence and computing `storage.DerivePoolPath(id)` for the next expected ID (`internal/gitaly/storage/repository_path.go:57-59`, `internal/praefect/router_per_repository.go:479-490`).
2. Issue a repository-creation RPC (e.g. `CreateRepository`) targeting that exact relative path before the victim's project is forked/needs a pool.
3. When the victim's project is later forked and `CreateObjectPool` is invoked with the same predicted relative path, `repoutil.Create`'s pre-existence check (`internal/gitaly/repoutil/create.go:96-104`) or `objectpool.Create`'s target-path check (`internal/git/objectpool/create.go:42-48`) will find the squatted repository and return `AlreadyExists`/`FailedPrecondition`, permanently preventing the object pool from ever being created for that repository network.

### Citations

**File:** internal/gitaly/storage/repository_path.go (L20-43)
```go
	praefectPoolDirRegexp = regexp.MustCompile(praefectPoolPathPrefix + `/[0-9a-f]{2}/[0-9a-f]{2}/[0-9]+$`)
	// railsPoolDirRegexp is used to validate object pool directory structure and name as generated by Rails.
	railsPoolDirRegexp = regexp.MustCompile(`@pools/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.git$`)
)

// IsRailsPoolRepository returns whether the repository is a pool repository generated by Rails.
func IsRailsPoolRepository(repo Repository) bool {
	matches := railsPoolDirRegexp.FindStringSubmatch(repo.GetRelativePath())
	if matches == nil || !strings.HasPrefix(matches[3], matches[1]+matches[2]) {
		return false
	}

	return true
}

// IsPraefectPoolRepository returns whether the repository is a Praefect generated object pool repository.
func IsPraefectPoolRepository(repo Repository) bool {
	return praefectPoolDirRegexp.MatchString(repo.GetRelativePath())
}

// IsPoolRepository returns whether the repository is an object pool.
func IsPoolRepository(repo Repository) bool {
	return IsRailsPoolRepository(repo) || IsPraefectPoolRepository(repo)
}
```

**File:** internal/gitaly/storage/repository_path.go (L52-68)
```go
// DerivePoolPath derives an object pools's disk storage path from its repository ID. The repository ID
// is hashed with SHA256 and the first four hex digits of the hash are used as the two subdirectories to
// ensure even distribution into subdirectories. The format is @cluster/pools/ab/cd/<repository-id>. The pools
// have a different directory prefix from other repositories so Gitaly can identify them in OptimizeRepository
// and avoid pruning them.
func DerivePoolPath(repositoryID int64) string {
	return deriveDiskPath(praefectPoolPathPrefix, repositoryID)
}

func deriveDiskPath(prefixDir string, repositoryID int64) string {
	hasher := sha256.New()
	// String representation of the ID is used to make it easier to derive the replica paths with
	// external tools. The error is ignored as the hash.Hash interface is documented to never return
	// an error.
	hasher.Write([]byte(strconv.FormatInt(repositoryID, 10)))
	hash := hasher.Sum(nil)
	return filepath.Join(prefixDir, fmt.Sprintf("%x/%x/%d", hash[0:1], hash[1:2], repositoryID))
```

**File:** internal/gitaly/service/objectpool/create.go (L17-30)
```go
func (s *server) CreateObjectPool(ctx context.Context, in *gitalypb.CreateObjectPoolRequest) (*gitalypb.CreateObjectPoolResponse, error) {
	if in.GetOrigin() == nil {
		return nil, errMissingOriginRepository
	}

	poolRepo := in.GetObjectPool().GetRepository()
	if poolRepo == nil {
		return nil, errMissingPool
	}

	if !storage.IsPoolRepository(poolRepo) {
		return nil, errInvalidPoolDir
	}

```

**File:** internal/git/objectpool/create.go (L42-48)
```go
	if _, err := os.Stat(objectPoolPath); err == nil {
		return nil, structerr.NewFailedPrecondition("target path exists already").
			WithMetadata("object_pool_path", objectPoolPath)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, structerr.NewInternal("checking object pool existence: %w", err).
			WithMetadata("object_pool_path", objectPoolPath)
	}
```

**File:** internal/praefect/router_per_repository.go (L474-490)
```go
	assignedNodes, err := r.assignRepositoryToNodes(virtualStorage, additionalRepoMetadata)
	if err != nil {
		return RepositoryMutatorRoute{}, err
	}

	id, err := r.rs.ReserveRepositoryID(ctx, virtualStorage, relativePath)
	if err != nil {
		return RepositoryMutatorRoute{}, fmt.Errorf("reserve repository id: %w", err)
	}

	replicaPath := storage.DeriveReplicaPath(id)
	if storage.IsRailsPoolRepository(&gitalypb.Repository{
		StorageName:  virtualStorage,
		RelativePath: relativePath,
	}) {
		replicaPath = storage.DerivePoolPath(id)
	}
```
