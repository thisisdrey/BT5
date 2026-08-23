Based on my investigation, I found a plausible analog in Gitaly's object-pool/alternates isolation logic, but I was not able to fully confirm the write path within the remaining tool budget. I'll present what I verified and flag the uncertainty.

### Title
Object pool linkage and disconnect logic validate only the first `objects/info/alternates` entry while Git itself honors all entries - (File: internal/git/objectpool/link.go, internal/git/objectpool/pool.go, internal/git/objectpool/disconnect.go)

### Summary
Several functions in the `objectpool` package read a repository's `objects/info/alternates` file via `stats.AlternatesInfoForRepository` and then only look at the **first** entry (`ObjectDirectories[0]` / `AbsoluteObjectDirectories()[0]`) to determine/validate the repository's pool relationship, while raw Git itself will use **every** line in that file as an alternate object source when resolving objects.

### Finding Description
`linkedToRepository` in [1](#0-0)  reads the alternates info and only inspects `altInfo.ObjectDirectories[0]` to decide whether the repository is already correctly linked to the expected pool, never checking whether `len(altInfo.ObjectDirectories) > 1`.

Similarly, `FromRepo` in [2](#0-1)  takes only `altInfo.AbsoluteObjectDirectories()[0]` to compute and validate the `ObjectPool` proto/relative path, ignoring any additional entries.

`Disconnect` in [3](#0-2)  is the one place in this package that does explicitly reject multiple alternate directories (`errors.New("multiple alternate object directories")`), which shows the maintainers are aware that repositories are expected to have at most one alternate — but this defensive check is not applied consistently across the package's other entry points (`link.go`, `pool.go`).

By contrast, `gitstorage.ReadAlternatesFile` used by the transaction/partition-assignment code path explicitly returns `ErrMultipleAlternates` when more than one alternate line exists [4](#0-3) , and `getAlternatePartitionID` in `partition_assigner.go` additionally validates the alternate path stays within storage bounds via `storage.ValidateRelativePath` [5](#0-4) . This confirms that "multiple alternates" is treated elsewhere in the codebase as an invalid/dangerous repository state that must be rejected — but `objectpool.linkedToRepository` and `objectpool.FromRepo` do not perform this rejection or the storage-bound validation for anything beyond the first entry.

Underlying this, `stats.AlternatesInfoForRepository`/`ReadAlternatesFile` in [6](#0-5)  parses *all* non-empty, non-comment lines into `ObjectDirectories` without limiting to one, meaning nothing stops a repository from having a well-formed, "linked" first alternate (pointing at the legitimate pool) plus one or more additional, completely unvalidated alternate directories.

### Impact Explanation
Git itself reads and uses **every** entry in `objects/info/alternates` when resolving objects for a repository (this is standard Git alternates behavior — it is not scoped to just the first line). If a repository under Gitaly's management ends up with more than one alternates entry, Gitaly's own bookkeeping (`linkedToRepository`, `FromRepo`) will believe the repository is correctly and solely linked to its expected pool because the first line matches, while any additional line — potentially pointing to a directory outside of the intended pool or even outside the storage — remains completely outside of Gitaly's validation, quota tracking, and authorization boundary for that pool relationship. This maps to the "object-pool and alternates isolation" concern explicitly called out in scope, and mirrors the reported bug class: a function is expected to process exactly one input(alternate) but silently tolerates and forwards extra, unauthorized ones to the underlying processing engine (Git).

### Likelihood Explanation
Likelihood depends on whether an ordinary user (via push, fork, replication, or snapshot-restore flows) can cause a Gitaly-managed repository to end up with a multi-line `objects/info/alternates` file. I confirmed this is explicitly guarded against and rejected in the transactional partition-assignment path (`gitstorage.ReadAlternatesFile` / `ErrMultipleAlternates`), and in `objectpool.Disconnect`, but I was **not able to fully verify**, within the remaining investigation budget, whether the same multi-alternate content could still reach `linkedToRepository`/`FromRepo` through paths such as `ReplicateRepository`, `CreateRepositoryFromSnapshot`, or fork creation before/without going through the guarded transactional code path. This is a genuine gap in my verification, not a confirmed exploit chain.

### Recommendation
Apply the same defensive checks used in `objectpool.Disconnect` and `gitstorage.ReadAlternatesFile` (reject `len(ObjectDirectories) > 1`, i.e. surface/propagate `ErrMultipleAlternates`-equivalent) consistently in `objectpool.linkedToRepository` and `objectpool.FromRepo`, and additionally validate any accepted alternate path stays within storage bounds (mirroring `storage.ValidateRelativePath` in `partition_assigner.go`) before establishing or trusting a pool linkage.

### Proof of Concept
Not established — I could not confirm, within the available tool budget, a concrete unprivileged write path (e.g., via `ReplicateRepository`, `CreateRepositoryFromSnapshot`, or `CreateFork`) that lets an ordinary user cause a target repository's `objects/info/alternates` file to contain more than one line before `objectpool.linkedToRepository`/`FromRepo` process it. This would need to be verified with full repository access (e.g. via a Devin session) to confirm exploitability rather than just the code-level inconsistency identified above.

### Citations

**File:** internal/git/objectpool/link.go (L169-203)
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
```

**File:** internal/git/objectpool/pool.go (L144-168)
```go
	altInfo, err := stats.AlternatesInfoForRepository(repoPath)
	if err != nil {
		return nil, fmt.Errorf("getting alternates info: %w", err)
	}

	if !altInfo.Exists || len(altInfo.ObjectDirectories) == 0 {
		return nil, ErrAlternateObjectDirNotExist
	}

	absolutePoolObjectDirPath := altInfo.AbsoluteObjectDirectories()[0]
	relativePoolObjectDirPath, err := filepath.Rel(storagePath, absolutePoolObjectDirPath)
	if err != nil {
		return nil, err
	}

	objectPoolProto := &gitalypb.ObjectPool{
		Repository: &gitalypb.Repository{
			StorageName:  repo.GetStorageName(),
			RelativePath: filepath.Dir(relativePoolObjectDirPath),
		},
	}

	if locator.ValidateRepository(ctx, objectPoolProto.GetRepository()) != nil {
		return nil, ErrInvalidPoolRepository
	}
```

**File:** internal/git/objectpool/disconnect.go (L69-87)
```go
	// A repository should only ever be linked to a single alternate object directory. If the
	// repository links to multiple object directories, the repository is in an invalid state.
	if len(altInfo.ObjectDirectories) > 1 {
		return errors.New("multiple alternate object directories")
	}

	// If the alternate object directory entry does not exist on disk, the repository's Git
	// alternates file is in an invalid state.
	altObjectDir := altInfo.AbsoluteObjectDirectories()[0]
	altObjectDirStats, err := os.Stat(altObjectDir)
	if err != nil {
		return err
	}

	// If the alternate object directory is not a directory, the repository's Git alternates file is
	// in an invalid state.
	if !altObjectDirStats.IsDir() {
		return errors.New("alternate object entry is not a directory")
	}
```

**File:** internal/gitaly/storage/gitstorage/alternates.go (L32-38)
```go
	if len(alternates) == 0 {
		return "", ErrNoAlternate
	} else if len(alternates) > 1 {
		// Repositories shouldn't have more than one alternate given they should only be
		// linked to a single pool at most.
		return "", ErrMultipleAlternates
	}
```

**File:** internal/gitaly/storage/storagemgr/partition_assigner.go (L330-342)
```go
	// The relative path should point somewhere within the same storage.
	alternateRelativePath, err := storage.ValidateRelativePath(
		pa.storagePath,
		// Take the relative path to the repository, not 'repository/objects'.
		filepath.Dir(
			// The path in alternates file points to the object directory of the alternate
			// repository. The path is relative to the repository's own object directory.
			filepath.Join(relativePath, "objects", alternate),
		),
	)
	if err != nil {
		return 0, fmt.Errorf("validate relative path: %w", err)
	}
```

**File:** internal/git/stats/repository_info.go (L613-642)
```go
func ReadAlternatesFile(repoPath string) ([]string, error) {
	file, err := os.Open(AlternatesFilePath(repoPath))
	if err != nil {
		return nil, fmt.Errorf("open: %w", err)
	}
	defer file.Close()

	var alternatePaths []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Bytes()

		switch {
		case len(line) == 0:
			// Empty lines are skipped by Git.
			continue
		case bytes.HasPrefix(line, []byte("#")):
			// Lines starting with a '#' are comments and thus need to be skipped.
			continue
		default:
			alternatePaths = append(alternatePaths, scanner.Text())
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scanning alternate paths: %w", err)
	}

	return alternatePaths, nil
}
```
