### Title
Missing path-escape validation of `GitAlternateObjectDirectories` allows cross-storage object disclosure - (File: internal/git/alternates/alternates.go)

### Summary
The external report describes `ExchangeIssuanceModule` failing to validate a caller-supplied list (duplicate/empty entries), letting an invariant be silently violated. The closest unprivileged analog in Gitaly is the `git_alternate_object_directories` repeated-string field on the `Repository` protobuf message. This field is taken directly from RPC requests and joined onto the repository path to build the `GIT_ALTERNATE_OBJECT_DIRECTORIES` environment variable for every Git subprocess, without any check that the resulting paths stay inside the repository/storage root.

### Finding Description
When Gitaly spawns a Git command for a repository, `ExecCommandFactory.newCommand` builds the process environment via: [1](#0-0) 

which calls: [2](#0-1) 

`alternates.Env` performs a raw `filepath.Join(repoPath, dir)` for every entry in `alternateObjectDirectories` — it never checks whether the joined path escapes the repository or the storage root. This is in stark contrast to the *on-disk* alternates handling used elsewhere, which explicitly rejects escapes: [3](#0-2) 

The `GitAlternateObjectDirectories` field on `Repository` is populated by GitLab Rails and echoed back to Gitaly on subsequent RPCs (e.g. access-check calls made from within `pre-receive`) to expose quarantined objects: [4](#0-3) 

The custom-hooks code path performs the identical unguarded join: [5](#0-4) 

While mutator RPCs are blocked from carrying a quarantine/alternate configuration (`ErrQuarantineConfiguredOnMutator`), accessor RPCs (e.g. `FindCommits`, `CommitIsAncestor`, blob/LFS listing, etc.) accept and use this field as shown by tests such as: [6](#0-5) [7](#0-6) 

Nowhere along this path is `storage.ValidateRelativePath` (used elsewhere, e.g. for `GetGitObjectDirectory` in `ObjectDirectoryPath`) applied to `GitAlternateObjectDirectories` entries: [8](#0-7) 

Because the field is a `repeated string` with no format/length/uniqueness or root-containment constraints (directly analogous to the unchecked `_sendTokens`/`_sendTokenAmounts` arrays in the external report), a caller can set an entry such as `"../../../../"` or an absolute path to a sibling repository/storage, causing Git to search for objects outside the intended repository when Gitaly executes any accessor Git command (e.g. `cat-file`, `rev-list`, `is-ancestor`) against that repository.

### Impact Explanation
This allows cross-repository/cross-storage object access: an attacker-controlled `Repository.GitAlternateObjectDirectories` value can cause Git subprocesses to treat an arbitrary filesystem path as part of the object search path. Combined with existing accessor RPCs (e.g. object-existence/`IsAncestor`/blob-retrieval endpoints), this can be used as an oracle to probe for the existence of, or exfiltrate content from, objects in repositories the caller should not have access to, since Git will happily read blobs/commits/trees found via any alternate directory in the search path regardless of which Gitaly-managed repository they belong to.

### Likelihood Explanation
The field is reachable directly through numerous accessor gRPC calls without any authorization-independent path check; any caller able to invoke an accessor RPC with a custom `Repository` message (including via the Rails access-check callback path documented in `doc/object_quarantine.md`) can set this value. No additional privilege is required beyond what is already needed to invoke ordinary read RPCs, which matches an unprivileged, single-request attack surface.

### Recommendation
Validate every entry of `GitAlternateObjectDirectories` (and `GitObjectDirectory`) the same way the on-disk alternates file is validated in `internal/git/dirs.go` — i.e., resolve each entry relative to the repository, reject absolute paths and `..`-escapes via `storage.ValidateRelativePath`, and reject duplicate/empty lists — before constructing the `GIT_ALTERNATE_OBJECT_DIRECTORIES`/`GIT_OBJECT_DIRECTORY` environment in `internal/git/alternates/alternates.go` and `internal/gitaly/hook/custom.go`.

### Proof of Concept
1. Issue any accessor RPC that accepts a full `Repository` message and forwards it to a Git command, e.g. `CommitIsAncestor`, `FindCommits`, `ListAllBlobs`.
2. Set `Repository.GitAlternateObjectDirectories = ["../../../victim-repo.git/objects"]` (or an absolute path) alongside a valid `storage_name`/`relative_path`.
3. Gitaly builds `GIT_ALTERNATE_OBJECT_DIRECTORIES=<repoPath>/../../../victim-repo.git/objects` via `alternates.Env` (`internal/git/alternates/alternates.go:9-27`) with no escape check, unlike `internal/git/dirs.go:74-85`.
4. The spawned Git process can now resolve/return objects that exist only in `victim-repo.git`, even though the caller only has access to the originally specified repository.

### Citations

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

**File:** internal/git/alternates/alternates.go (L9-27)
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

	return env
}
```

**File:** internal/git/dirs.go (L74-85)
```go
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
```

**File:** proto/go/gitalypb/shared.pb.go (L256-259)
```go
	// git_alternate_object_directories sets the GIT_ALTERNATE_OBJECT_DIRECTORIES envvar on git commands to
	// the values of this field. It influences the list of Git object directories which can be used to search
	// for Git objects.
	GitAlternateObjectDirectories []string `protobuf:"bytes,5,rep,name=git_alternate_object_directories,json=gitAlternateObjectDirectories,proto3" json:"git_alternate_object_directories,omitempty"`
```

**File:** internal/gitaly/hook/custom.go (L180-190)
```go
	alternateObjectDirectories := env.ExtractValue(envs, "GIT_ALTERNATE_OBJECT_DIRECTORIES")
	if alternateObjectDirectories == "" && len(payload.Repo.GetGitAlternateObjectDirectories()) != 0 {
		var absolutePaths []string
		for _, alternateObjectDirectory := range payload.Repo.GetGitAlternateObjectDirectories() {
			absolutePaths = append(absolutePaths, filepath.Join(repoPath, alternateObjectDirectory))
		}
		alternateObjectDirectories = strings.Join(absolutePaths, ":")
	}
	if alternateObjectDirectories != "" {
		customEnvs = append(customEnvs, "GIT_ALTERNATE_OBJECT_DIRECTORIES="+alternateObjectDirectories)
	}
```

**File:** internal/gitaly/service/commit/find_commits_test.go (L828-852)
```go
	repo, repoPath := gittest.CreateRepository(t, ctx, cfg)
	altObjectsDir := "./alt-objects"
	commitID := gittest.WriteCommit(t, cfg, repoPath,
		gittest.WithAlternateObjectDirectory(filepath.Join(repoPath, altObjectsDir)),
	)

	for _, tc := range []struct {
		desc          string
		altDirs       []string
		expectedCount int
		expectedErr   error
	}{
		{
			desc:          "present GIT_ALTERNATE_OBJECT_DIRECTORIES",
			altDirs:       []string{altObjectsDir},
			expectedCount: 1,
		},
		{
			desc:          "empty GIT_ALTERNATE_OBJECT_DIRECTORIES",
			altDirs:       []string{},
			expectedCount: 0,
			expectedErr: structerr.NewNotFound("commits not found").
				WithDetail(&gitalypb.FindCommitsError{}),
		},
	} {
```

**File:** internal/gitaly/service/commit/isancestor_test.go (L195-219)
```go
			desc: "with alternates directory",
			setup: func(t *testing.T) setupData {
				repo, repoPath := gittest.CreateRepository(t, ctx, cfg)

				ancestor := gittest.WriteCommit(t, cfg, repoPath)

				altObjectsDir := "./alt-objects"
				child := gittest.WriteCommit(t, cfg, repoPath,
					gittest.WithParents(ancestor),
					gittest.WithAlternateObjectDirectory(filepath.Join(repoPath, altObjectsDir)),
				)
				repo.GitAlternateObjectDirectories = []string{altObjectsDir}

				return setupData{
					request: &gitalypb.CommitIsAncestorRequest{
						Repository: repo,
						AncestorId: ancestor.String(),
						ChildId:    child.String(),
					},
					expectedResponse: &gitalypb.CommitIsAncestorResponse{
						Value: true,
					},
				}
			},
		},
```

**File:** internal/git/localrepo/paths.go (L19-41)
```go
// ObjectDirectoryPath returns the full path of the object directory. The errors returned are gRPC
// errors with relevant error codes and should be passed back to gRPC without further decoration.
func (repo *Repo) ObjectDirectoryPath(ctx context.Context) (string, error) {
	repoPath, err := repo.Path(ctx)
	if err != nil {
		return "", err
	}

	objectDirectoryPath := repo.GetGitObjectDirectory()
	if objectDirectoryPath == "" {
		return "", structerr.NewInvalidArgument("object directory path is not set")
	}

	storagePath, err := repo.locator.GetStorageByName(ctx, repo.GetStorageName())
	if err != nil {
		return "", fmt.Errorf("get storage by name: %w", err)
	}

	// Ensure the path points somewhere in the storage.
	relativeObjectDirectoryPath, err := storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))
	if err != nil {
		return "", structerr.NewInvalidArgument("validate relative path: %w", err)
	}
```
