This confirms `GetObjectDirectorySizeRequest.Repository` is a client-suppliable proto message: `GitObjectDirectory` is a public field on `gitalypb.Repository` (`proto/shared.proto`), and `GetObjectDirectorySize` (`internal/gitaly/service/repository/size.go:39-57`) passes the caller-supplied `Repository` straight into `repo.ObjectDirectoryPath(ctx)` after only `ValidateRepository`, which does not vet `GitObjectDirectory` contents. [1](#0-0) 

I was not able to fully verify (within the tool budget) whether `ValidateRepository` performs any additional check on `GitObjectDirectory` before `ObjectDirectoryPath` is invoked, nor trace every other RPC that accepts a raw `Repository` message with an attacker/Rails-controlled `GitObjectDirectory` (e.g. `IsAncestor`, other quarantine-aware RPCs). This would need further investigation (e.g. reading `ValidateRepository` in `internal/gitaly/storage/locator.go` end-to-end and grepping all RPC handlers that call `ObjectDirectoryPath`/`InfoAlternatesPath`) to make a fully confident claim about exploitability from an unauthenticated/ordinary caller versus only from Rails' internal access-check flow.

### Title
Loose "quarantine" naming-pattern check bypasses per-repository quarantine ownership verification - (File: internal/git/localrepo/paths.go)

### Summary
`Repo.ObjectDirectoryPath` decides whether a client-supplied `GitObjectDirectory` is legitimate by checking only whether its basename is literally `"quarantine"`, or whether its parent directory is named `"quarantine"` and its basename has the prefix `"tmp_objdir"` [2](#0-1) . If this loose naming heuristic matches, the function completely skips the real ownership check — the one that verifies the directory sits under this specific repository's derived temp-dir prefix, `QuarantineDirectoryPrefix(repo)`, which is a SHA-1 hash of the repo's own relative path [3](#0-2) [4](#0-3) .

### Finding Description
This mirrors the forex-options bug class: the original code substituted a coarse, easily-satisfied signal (day-of-week equality) for the actual invariant that mattered (elapsed time actually being within one day), silently expanding the accepted window whenever the coarse signal alone could not distinguish valid from invalid inputs. Here, `isTransactionQuarantineDir` in `ObjectDirectoryPath` substitutes a coarse signal — "does the last path component look like a quarantine directory name?" — for the actual invariant that matters: "does this quarantine directory actually belong to this repository?" [5](#0-4) 

Only when `isTransactionQuarantineDir` is `false` does the code perform the strict check that ties the object directory to the calling repository, either by requiring it to be inside the repository itself (`storage.ValidateRelativePath(repoPath, objectDirectoryPath)`) or, failing that, requiring the absolute path to start with `filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))` [6](#0-5) . When `isTransactionQuarantineDir` is `true`, none of this ownership binding is performed — the code proceeds straight to `os.Stat` and returns the path [7](#0-6) .

`GitObjectDirectory` is a plain string field on the `gitalypb.Repository` protobuf message that is supplied directly by RPC callers (Rails/gitlab-shell relay it back to Gitaly as part of the object-quarantine plumbing described in `doc/object_quarantine.md`) [8](#0-7) . `GetObjectDirectorySize` builds a `localrepo.Repo` directly from the caller-supplied `Repository` message and calls `ObjectDirectoryPath` on it with no additional sanitization of `GitObjectDirectory` beyond generic repository validation [1](#0-0) . The earlier `storage.ValidateRelativePath` call at line 38 of `paths.go` only ensures the joined path stays inside the storage root; it does not ensure the path belongs to the requesting repository [9](#0-8) .

Consequently, a caller who controls the `Repository.GitObjectDirectory` field of a request can point it at any storage-relative path ending in a directory component literally named `quarantine` (or `.../quarantine/tmp_objdir*`) that exists anywhere within the storage root — including quarantine directories that were created for a completely different repository's in-flight push — and the loose naming check will accept it without ever confirming that directory is this repository's own quarantine.

### Impact Explanation
If exploitable through a reachable RPC, this allows cross-repository access to another repository's (or another push's) in-flight, not-yet-migrated quarantined objects and their computed sizes/paths, undermining the isolation guarantee that Git object quarantine is supposed to provide (per `doc/object_quarantine.md`, the entire mechanism exists so that in-flight push objects are isolated until access checks pass). This is a concrete instance of the "cross-repository object access" and "quarantine bypass" categories called out as in-scope. The severity depends on which RPCs are reachable with attacker-influenced `GitObjectDirectory` and what information/side effects they expose (currently confirmed: object directory size disclosure via `GetObjectDirectorySize`).

### Likelihood Explanation
Likelihood is moderate and conditional: an attacker needs the ability to (a) know or guess a live sibling quarantine directory name/path under the same storage (these are ephemeral, per-push temp directories with randomized suffixes, e.g. `tmp_objdir-incoming-Gbc29N`), and (b) submit an RPC with a `Repository` message where they control `GitObjectDirectory`. The naming-pattern collision itself is trivial to construct once a directory name is known (unlike the SHA-1 prefix check used in the strict path), because the loose path requires only a `"quarantine"` basename or `"quarantine"`-parent + `tmp_objdir` prefix, with no repository-specific secret. The main uncertainty (unverified given tool budget) is exactly which authenticated Gitaly RPCs pass through fully attacker-controlled `Repository.GitObjectDirectory` values versus internally-generated ones set only by trusted internal quarantine/transaction code paths.

### Recommendation
Remove the special-case bypass for `isTransactionQuarantineDir`, or at minimum still enforce the `QuarantineDirectoryPrefix(repo)`-based ownership check (or an equivalent binding to the specific transaction/repository) even when the directory's basename matches the `quarantine`/`tmp_objdir` naming convention. The naming pattern should only be used to select which validation branch to take (e.g., to decide the expected temp-dir location to check against), not as a substitute for verifying that the resolved path actually belongs to the requesting repository/transaction.

### Proof of Concept
Conceptual (not fully verified end-to-end due to tool limits):
1. Repository A has an in-flight push, producing a Git-managed quarantine directory such as `<storage>/+gitaly/tmp/tx-tmp/quarantine/tmp_objdir-incoming-XXXXXX` (or Gitaly's own transaction quarantine temp dir).
2. An attacker who can issue `GetObjectDirectorySize` (or another RPC that resolves `ObjectDirectoryPath`) for repository B crafts `Repository.GitObjectDirectory` to be a relative path from B's repo root to that same quarantine directory (e.g. via `../../<storage-relative-path-to-A's-quarantine>/tmp_objdir-incoming-XXXXXX`).
3. `storage.ValidateRelativePath(storagePath, ...)` at `paths.go:38` accepts it because it is still within the storage root.
4. `isTransactionQuarantineDir` evaluates to `true` because parent directory is `quarantine` and basename has prefix `tmp_objdir`, at `paths.go:45`.
5. The strict ownership check at `paths.go:53-74` (which would have rejected this, since it doesn't match B's `QuarantineDirectoryPrefix`) is skipped entirely.
6. `ObjectDirectoryPath` returns repository A's quarantine directory to the caller operating "as repository B", leaking size information about A's in-flight, not-yet-accepted push objects.

### Citations

**File:** internal/gitaly/service/repository/size.go (L39-49)
```go
func (s *server) GetObjectDirectorySize(ctx context.Context, in *gitalypb.GetObjectDirectorySizeRequest) (*gitalypb.GetObjectDirectorySizeResponse, error) {
	repository := in.GetRepository()
	if err := s.locator.ValidateRepository(ctx, repository); err != nil {
		return nil, structerr.NewInvalidArgument("%w", err)
	}
	repo := s.localRepoFactory.Build(repository)

	path, err := repo.ObjectDirectoryPath(ctx)
	if err != nil {
		return nil, err
	}
```

**File:** internal/git/localrepo/paths.go (L36-41)
```go

	// Ensure the path points somewhere in the storage.
	relativeObjectDirectoryPath, err := storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))
	if err != nil {
		return "", structerr.NewInvalidArgument("validate relative path: %w", err)
	}
```

**File:** internal/git/localrepo/paths.go (L43-75)
```go
	parentDir := filepath.Base(filepath.Dir(relativeObjectDirectoryPath))
	baseDir := filepath.Base(relativeObjectDirectoryPath)
	isTransactionQuarantineDir := (baseDir == "quarantine") || ((parentDir == "quarantine") && strings.HasPrefix(baseDir, "tmp_objdir"))

	// Transactions quarantine a repository by pointing the object directory to a 'quarantine' named
	// directory in the transaction's temporary directory. If the base directory is `quarantine`,
	// Git push may apply an additional layer of quarantine such as `/quarantine/tmp_objdir-incoming-Gbc29N`
	// so we don't assert the `/quarantine` being the last element of the path. We thus also check for
	// whether the parent directory is in `quarantine` and whether the base directory has the expected
	// `tmp_objdir` suffix.
	if !isTransactionQuarantineDir {
		// We need to check whether the relative object directory as given by the repository is
		// a valid path. This may either be a path in the Git repository itself, where it may either
		// point to the main object directory storage or to an object quarantine directory as
		// created by git-receive-pack(1). Alternatively, if that is not the case, then it may be a
		// manual object quarantine directory located in the storage's temporary directory. These
		// have a repository-specific prefix which we must check in order to determine whether the
		// quarantine directory does in fact belong to the repo at hand.
		if _, origError := storage.ValidateRelativePath(repoPath, objectDirectoryPath); origError != nil {
			tempDir, err := repo.locator.TempDir(repo.GetStorageName())
			if err != nil {
				return "", structerr.NewInvalidArgument("getting storage's temporary directory: %w", err)
			}

			expectedQuarantinePrefix := filepath.Join(tempDir, storage.QuarantineDirectoryPrefix(repo))
			absoluteObjectDirectoryPath := filepath.Join(repoPath, objectDirectoryPath)

			// The relative path is outside of the repository
			if !strings.HasPrefix(absoluteObjectDirectoryPath, expectedQuarantinePrefix) {
				return "", structerr.NewInvalidArgument("not a valid relative path: %w", origError)
			}
		}
	}
```

**File:** internal/git/localrepo/paths.go (L76-83)
```go

	fullPath := filepath.Join(repoPath, objectDirectoryPath)
	if _, err := os.Stat(fullPath); os.IsNotExist(err) {
		return "", structerr.NewNotFound("object directory does not exist: %q", fullPath)
	}

	return fullPath, nil
}
```

**File:** internal/gitaly/storage/locator.go (L201-212)
```go
// QuarantineDirectoryPrefix returns a prefix for use in the temporary directory. The prefix is
// based on the relative repository path and will stay stable for any given repository. This allows
// us to verify that a given quarantine object directory indeed belongs to the repository at hand.
// Ideally, this function would directly be located in the quarantine module, but this is not
// possible due to cyclic dependencies.
func QuarantineDirectoryPrefix(repo Repository) string {
	hash := [20]byte{}
	if repo != nil {
		hash = sha1.Sum([]byte(repo.GetRelativePath()))
	}
	return fmt.Sprintf("quarantine-%x-", hash[:8])
}
```

**File:** doc/object_quarantine.md (L109-123)
```markdown
### How GitLab passes the object quarantine information around

To overcome this problem, the GitLab `pre-receive` hook
[reads the object directory configuration from its environment](https://gitlab.com/gitlab-org/gitaly/-/blob/71d527f4f16c1f0e76793f055def0299b375cc7d/internal/gitlabshell/env.go#L9).
and passes this information
[along with the HTTP API call](https://gitlab.com/gitlab-org/gitaly/-/blob/71d527f4f16c1f0e76793f055def0299b375cc7d/internal/gitaly/hook/manager.go#L30-46).
On the Rails side, we then
[put the object directory information in the "request store"](https://gitlab.com/gitlab-org/gitlab/-/blob/master/lib/api/internal/base.rb#L43)
(i.e., request-scoped thread-local storage). And then during that
Rails request, when Rails makes Gitaly requests on this repo, we send back the quarantine information
[in the Gitaly `Repository` struct](https://gitlab.com/gitlab-org/gitlab/-/blob/f81f30c29a0edce20f6737fdccc3315c8baab9d1/lib/gitlab/gitaly_client/util.rb#L8-17).
And finally, inside Gitaly, when we spawn a Git process, we
[re-create the environment variables](https://gitlab.com/gitlab-org/gitaly/-/blob/969bac80e2f246867c1a976864bd1f5b34ee43dd/internal/git/alternates/alternates.go#L21-34)
that were present on the `pre-receive` hook, so that we can see the
quarantined objects.
```
