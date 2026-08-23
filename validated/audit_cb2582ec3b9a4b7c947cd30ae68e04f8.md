### Title
Object-pool `Link()` idempotency check validates only the first alternates entry while Gitaly's object resolution honors every entry, allowing a crafted multi-line `objects/info/alternates` file to grant persistent cross-repository object access - (File: internal/git/objectpool/link.go)

### Summary
`linkedToRepository()` in `internal/git/objectpool/link.go` decides whether a repository is "already linked" to an object pool by inspecting only `altInfo.ObjectDirectories[0]`, the first line of the `objects/info/alternates` file. Gitaly's actual object-resolution code path, `altObjectDirs()` in `internal/git/dirs.go`, instead walks and follows every non-empty, non-comment line of that same file (recursively, up to depth 5). This is the same class of bug as the BunniToken report: a validation/gating function inspects a narrower view of the state than the function that actually consumes/acts on that state, so the gate can "pass" while unvalidated, effectively-active data grants unintended capability.

### Finding Description
`Link()` (internal/git/objectpool/link.go:28-84) is the routine gitaly-ruby/Rails calls (during fork/dedup housekeeping) to attach a repository to its object pool. Before writing anything, it calls `linkedToRepository()` (lines 168-204): [1](#0-0) 

Only the first entry (`altInfo.ObjectDirectories[0]`) is compared against the expected pool path. If it matches, `linkedToRepository` returns `true`, and `Link()` treats the repository as already correctly linked - it never rewrites, sanitizes, or truncates the alternates file: [2](#0-1) 

However, the code that Gitaly actually uses at runtime to resolve which directories are valid alternate object stores for a repository, `altObjectDirs()`, does not stop at the first line - it iterates over every line of the alternates file: [3](#0-2) 

The only guard applied to each additional line is that it must resolve to a path with the `storagePrefix` (i.e., anywhere under the storage root) - there is no restriction limiting a repository to a single alternate directory at this layer. The invariant "a repository should only ever be linked to a single alternate object directory" is only enforced elsewhere, e.g. in `Disconnect()` (`internal/git/objectpool/disconnect.go:69-73`) and in `gitstorage.ReadAlternatesFile` (`internal/gitaly/storage/gitstorage/alternates.go:32-38`, returning `ErrMultipleAlternates`) - not in `Link()`/`linkedToRepository()`.

If a repository's `objects/info/alternates` file is populated (e.g. via a repository import/bundle upload that is allowed to include arbitrary loose repo content, or any other write path that lands content in that file before pool-linking housekeeping runs) with two lines:
```
<relative-path-to-legit-pool>/objects
<relative-path-to-some-other-repo>/objects
```
then `linkedToRepository()` sees `ObjectDirectories[0]` equal to the expected pool path, returns `true`, and `Link()` leaves the file untouched - silently accepting the second, unvalidated line. From that point on, every git operation against the repository (via `altObjectDirs`/`ObjectDirectories`/`AlternateObjectDirectories`, used throughout git command construction to set `GIT_ALTERNATE_OBJECT_DIRECTORIES`) will also honor that second line, giving the repository read access to another repository's objects anywhere under the same storage root.

### Impact Explanation
This produces cross-repository object disclosure within a storage: a repository that should only be able to read objects from its designated pool gains persistent, silent access to objects belonging to an arbitrary other repository on the same storage, because the pool-membership validation only checks the first alternates line while the object-resolution logic (`internal/git/dirs.go`) honors the full file. It also permanently corrupts the "single alternate" invariant relied on elsewhere (e.g. `Disconnect()` explicitly errors out on more than one `ObjectDirectories` entry), meaning normal disconnect/repair operations for that repository will subsequently fail.

### Likelihood Explanation
Exploitation requires an actor able to place a crafted multi-line `objects/info/alternates` file inside a repository before Gitaly's pool-linking housekeeping (`Link()`) runs against it - a scenario plausible for repository import/fork flows where repository content (including the `.git` directory's info files) is not fully re-derived by Gitaly but copied/extracted from user-supplied input. Because `Link()`'s idempotency check never re-validates or re-writes the file once it superficially matches on the first line, the malicious second entry survives indefinitely rather than being pruned on the next linking pass.

### Recommendation
Align the two checks: `linkedToRepository()` should validate that the alternates file contains exactly one entry and that it equals the expected pool relative path (mirroring the single-alternate invariant enforced in `Disconnect()`/`gitstorage.ReadAlternatesFile`), rejecting or rewriting the file when additional or mismatched entries are present, rather than treating any first-line match as "already linked."

### Proof of Concept
1. Import or otherwise create a repository whose `objects/info/alternates` file already contains two lines: the correct relative pool objects path, followed by a second line pointing at another repository's `objects` directory (both under the same storage root, satisfying `storagePrefix`).
2. Trigger the object-pool linking flow that calls `objectpool.Link(ctx, pool, repo, txManager)` for this repository/pool pair.
3. `linkedToRepository()` compares only `ObjectDirectories[0]`, finds it matches the expected pool path, and returns `true`; `Link()` returns early without touching the alternates file (internal/git/objectpool/link.go:39-53).
4. Any subsequent git operation on the repository resolves objects via `altObjectDirs()` (internal/git/dirs.go:45-96), which follows both lines, granting the repository read access to the second, unauthorized object directory.

### Citations

**File:** internal/git/objectpool/link.go (L39-53)
```go
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

**File:** internal/git/objectpool/link.go (L185-203)
```go
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

**File:** internal/git/dirs.go (L66-93)
```go
	alternates, err := os.ReadFile(filepath.Join(objDir, "info", "alternates"))
	if os.IsNotExist(err) {
		return dirs, nil
	}
	if err != nil {
		return nil, err
	}

	for _, newDir := range strings.Split(string(alternates), "\n") {
		if len(newDir) == 0 || newDir[0] == '#' {
			continue
		}

		if !filepath.IsAbs(newDir) {
			newDir = filepath.Join(objDir, newDir)
		}

		if !strings.HasPrefix(newDir, storagePrefix) {
			return nil, alternateOutsideStorageError(newDir)
		}

		nestedDirs, err := altObjectDirs(ctx, logger, storagePrefix, newDir, depth+1)
		if err != nil {
			return nil, err
		}

		dirs = append(dirs, nestedDirs...)
	}
```
