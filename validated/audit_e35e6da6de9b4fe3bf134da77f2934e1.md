Confirmed: `ValidateRepository` in `internal/gitaly/config/locator.go` only validates `StorageName` and `RelativePath` — it never inspects `GitObjectDirectory` / `GitAlternateObjectDirectories` at all. Those two fields flow straight from the client-supplied `gitalypb.Repository` message into `alternates.Env()` (`internal/git/gitcmd/command_factory.go:519`) which just does `filepath.Join(repoPath, objectDirectory)` with no bounds checking, unlike `localrepo.Repo.ObjectDirectoryPath()` (`internal/git/localrepo/paths.go`) which does enforce `storage.ValidateRelativePath`. This is the same root-cause shape as the Maple Loan bug: a security-critical parameter (here, the object-directory search path used to satisfy every object lookup for the git subprocess) is taken verbatim from attacker-controlled input instead of from an authoritative/validated source.

### Title
Unvalidated `GitObjectDirectory`/`GitAlternateObjectDirectories` request fields allow object-directory storage escape - (File: internal/git/gitcmd/command_factory.go)

### Summary
Any RPC whose request carries a `gitalypb.Repository` message lets the caller set `git_object_directory` and `git_alternate_object_directories`. These fields are meant only to be round-tripped by GitLab Rails during pre-receive quarantine handling, but Gitaly does not verify that the values actually originated from a legitimate quarantine created via `internal/git/quarantine`. When `ExecCommandFactory.newCommand` builds a git invocation, it unconditionally injects `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` from these raw fields via `alternates.Env()`, without the path-containment check that exists elsewhere (`localrepo.Repo.ObjectDirectoryPath`).

### Finding Description
`ValidateRepository` (`internal/gitaly/config/locator.go:47-118`) only checks `StorageName` and `RelativePath`; it never touches `GitObjectDirectory`/`GitAlternateObjectDirectories`. [1](#0-0) 

`alternates.Env` builds the environment by blindly joining the client-supplied directory strings onto the repo path: [2](#0-1) 

`ExecCommandFactory.newCommand` calls this helper directly with the request's raw fields for essentially every git subprocess Gitaly spawns: [3](#0-2) 

Because `filepath.Join` cleans `..` segments, a value such as `GitObjectDirectory: "../../../other-storage/other-repo.git/objects"` (or any relative traversal) resolves to an arbitrary directory outside the intended repository — and outside the storage root entirely — becoming the git object search path for that RPC's git process. A parallel, better-guarded implementation exists in `localrepo.Repo.ObjectDirectoryPath`, which explicitly calls `storage.ValidateRelativePath` and further restricts non-`objects/`-relative paths to a repository-specific quarantine-prefix under the storage temp dir: [4](#0-3) 

That validation, however, is not applied on the hot command-construction path used by `gitcmd.CommandFactory`, nor is it enforced generically for all RPCs. The transaction middleware even documents this gap: it explicitly acknowledges that these two fields "should only be configured on a repository coming from a request that was already configured with a quarantine directory... looped back to Gitaly from Rails' authorization checks," but only *mutator* RPCs are rejected when the fields are set — accessor (read-only) RPCs are allowed through unchanged: [5](#0-4) 

There is no cryptographic or session binding tying these fields to an actual, server-created quarantine (`quarantine.New`/`quarantine.Apply` in `internal/git/quarantine/quarantine.go`) — the trust boundary is purely "did the caller happen to send an otherwise-legitimate-looking value," exactly analogous to Maple Loan trusting `lender_`-supplied fee/address parameters without checking they came from an authoritative pool contract.

### Impact Explanation
An ordinary Gitaly client (any component or attacker with gRPC access, e.g. a malicious/compromised GitLab-Shell/Workhorse relay or a client hitting an accessor RPC directly) can force any read-oriented git subprocess for a repository they have access to, to instead search for and disclose objects from an arbitrary directory on the Gitaly host — including objects belonging to a different, unrelated repository or storage. This is a cross-repository object disclosure / storage-boundary escape: git commands (e.g. `cat-file`, `log`, blob/diff RPCs) executed against the "victim" repository would resolve object lookups against the attacker-chosen directory, potentially leaking blob/commit content the caller should not have access to.

### Likelihood Explanation
Reaching this path requires only sending a normal, unprivileged RPC (any accessor RPC accepting a `Repository` message) with the `git_object_directory`/`git_alternate_object_directories` fields populated with a traversal value — no special session state, quarantine, or pre-receive hook context is needed. The mutator-side guard (`ErrQuarantineConfiguredOnMutator`) does not apply to read RPCs, and `ValidateRepository`/`GetRepoPath` never inspect these fields, so likelihood of reachability is high for any deployment where Gitaly RPCs are reachable with attacker-influenced `Repository` messages (e.g. through a compromised or overly-trusted intermediary, or any RPC surface where these fields aren't stripped upstream).

### Recommendation
Do not trust `GitObjectDirectory`/`GitAlternateObjectDirectories` from arbitrary requests. Either (a) bind these fields to a server-issued quarantine token/session established via `quarantine.New`, verifying it server-side before injecting into `alternates.Env`, or (b) apply the same containment check used in `localrepo.Repo.ObjectDirectoryPath` (`storage.ValidateRelativePath` plus the quarantine-directory-prefix check) universally inside `gitcmd.ExecCommandFactory.newCommand` before constructing `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES`, rejecting any value that resolves outside the repository's own storage/quarantine scope for every RPC, not just mutators.

### Proof of Concept
1. Attacker has ordinary access to call a read-only Gitaly RPC (e.g. `ListBlobs`, `IsAncestor`) against a repository `A` they can access, supplying a `Repository` message with `relative_path` set to repo `A` but `git_object_directory` set to `"../../victim-storage/private-repo.git/objects"`.
2. `beginTransactionForRepository` treats this as a non-mutator accessor and does not reject it (`ErrQuarantineConfiguredOnMutator` only triggers for `OpMutator`) — see [6](#0-5) .
3. `ExecCommandFactory.newCommand` builds `alternates.Env(repoPath, "../../victim-storage/private-repo.git/objects", nil)`, producing `GIT_OBJECT_DIRECTORY=<repoPath>/../../victim-storage/private-repo.git/objects`, which `filepath.Join`/git resolves outside the storage root.
4. The subsequent git subprocess (e.g. `git cat-file`/`git log`) resolves object lookups against the victim repository's object directory, allowing the attacker to enumerate or read objects that belong to a repository they were never authorized to access.

### Citations

**File:** internal/gitaly/config/locator.go (L47-63)
```go
func (l *configLocator) ValidateRepository(ctx context.Context, repo storage.Repository, opts ...storage.ValidateRepositoryOption) error {
	var cfg storage.ValidateRepositoryConfig
	for _, opt := range opts {
		opt(&cfg)
	}

	// Only checking for `nil` isn't sufficient as Protobuf messages may be non-nil, but still
	// either invalid or empty. Thus we also explicitly verify whether both the storage name and
	// the relative path are unset.
	if repo == nil || repo.GetStorageName() == "" && repo.GetRelativePath() == "" {
		return structerr.NewInvalidArgument("%w", storage.ErrRepositoryNotSet)
	}

	relativePath := repo.GetRelativePath()
	if len(relativePath) == 0 {
		return structerr.NewInvalidArgument("%w", storage.ErrRepositoryPathNotSet)
	}
```

**File:** internal/git/alternates/alternates.go (L9-24)
```go
// Env returns the alternate object directory environment variables.
func Env(repoPath, objectDirectory string, alternateObjectDirectories []string) []string {
	var env []string
	if objectDirectory != "" {
		env = append(env, fmt.Sprintf("GIT_OBJECT_DIRECTORY=%s", filepath.Join(repoPath, objectDirectory)))
	}

	if len(alternateObjectDirectories) > 0 {
		var dirsList []string

		for _, dir := range alternateObjectDirectories {
			dirsList = append(dirsList, filepath.Join(repoPath, dir))
		}

		env = append(env, fmt.Sprintf("GIT_ALTERNATE_OBJECT_DIRECTORIES=%s", strings.Join(dirsList, ":")))
	}
```

**File:** internal/git/gitcmd/command_factory.go (L511-520)
```go
	var repoPath string
	if repo != nil {
		var err error
		repoPath, err = cf.locator.GetRepoPath(ctx, repo)
		if err != nil {
			return nil, err
		}

		env = append(alternates.Env(repoPath, repo.GetGitObjectDirectory(), repo.GetGitAlternateObjectDirectories()), env...)
	}
```

**File:** internal/git/localrepo/paths.go (L37-75)
```go
	// Ensure the path points somewhere in the storage.
	relativeObjectDirectoryPath, err := storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))
	if err != nil {
		return "", structerr.NewInvalidArgument("validate relative path: %w", err)
	}

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

**File:** internal/gitaly/storage/storagemgr/middleware.go (L271-297)
```go
	if targetRepo.GetGitObjectDirectory() != "" || len(targetRepo.GetGitAlternateObjectDirectories()) > 0 {
		// The object directories should only be configured on a repository coming from a request that
		// was already configured with a quarantine directory and is being looped back to Gitaly from Rails'
		// authorization checks. If that's the case, the request should already be running in scope of a
		// transaction and the repository rewritten to point to the snapshot repository. We thus don't start
		// a new transaction if we encounter this.
		//
		// This property is violated in tests which manually configure the object directory or the alternate
		// object directory. This allows for circumventing the transaction management by configuring the either
		// of the object directories. We'll leave this unaddressed for now and later address this by removing
		// the options to configure object directories and alternates in a request.

		if methodInfo.Operation == protoregistry.OpMutator {
			// Accessor requests may come with quarantine configured from Rails' access checks. Since the
			// RPC that triggered these access checks would already run in a transaction and target a
			// snapshot, we won't start another one. Mutators however are rejected to prevent writes
			// unintentionally targeting the main repository.
			return transactionalizedRequest{}, ErrQuarantineConfiguredOnMutator
		}

		rewrittenReq, err := restoreSnapshotRelativePath(ctx, methodInfo, req)
		if err != nil {
			return transactionalizedRequest{}, fmt.Errorf("restore snapshot relative path: %w", err)
		}

		return nonTransactionalRequest(ctx, rewrittenReq), nil
	}
```
