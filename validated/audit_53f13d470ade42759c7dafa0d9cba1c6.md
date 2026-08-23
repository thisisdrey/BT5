## Analysis Result

### Title
Unvalidated `git_object_directory` / `git_alternate_object_directories` RPC fields injected into custom-hook `GIT_OBJECT_DIRECTORY` environment without root-containment check - (File: `internal/gitaly/hook/custom.go`)

### Summary
`gitalypb.Repository` exposes two client-settable fields, `git_object_directory` and `git_alternate_object_directories`, which are documented to "set the `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` envvar on git commands" [1](#0-0) . These values flow unchanged into `gitcmd.HooksPayload.Repo` [2](#0-1)  and are later consumed by `customHooksEnv`, which builds the environment for admin-installed custom pre-receive/update/post-receive hooks:

```go
objectDirectory := env.ExtractValue(envs, "GIT_OBJECT_DIRECTORY")
if objectDirectory == "" && payload.Repo.GetGitObjectDirectory() != "" {
    objectDirectory = filepath.Join(repoPath, payload.Repo.GetGitObjectDirectory())
}
``` [3](#0-2) 

Unlike the equivalent, well-hardened `ObjectDirectoryPath` helper — which calls `storage.ValidateRelativePath` and additionally verifies the path sits under a repository- or transaction-specific quarantine prefix before trusting it [4](#0-3)  — `customHooksEnv` performs a bare `filepath.Join` with no root-containment check at all.

### Finding Description
`preReceiveHook` only re-derives `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` from the *actual* process environment when both are already present (the real `git-receive-pack` quarantine case) [5](#0-4) . When hooks are invoked outside of an actual `git-receive-pack` quarantine — e.g. via `UpdaterWithHooks.UpdateReference`, used by OperationService RPCs (`UserCommitFiles`, `UserMergeBranch`, `UserCherryPick`, etc.) — the repository object handed to `gitcmd.NewHooksPayload` is the raw, client-supplied `repoProto` whenever no quarantine directory is passed in for that call [6](#0-5) . Because `HooksPayload.Repo` is a straight field-copy of `repoProto` [2](#0-1) , an attacker-controlled `git_object_directory` string such as `"../../../../../../tmp/evil"` survives the JSON round-trip (`HooksPayloadFromEnv`) and reaches `customHooksEnv`, which joins it onto `repoPath` and exports it as `GIT_OBJECT_DIRECTORY` for whatever custom hook scripts an administrator has installed for that repository.

### Impact Explanation
If exploitable end-to-end, this would let a normal RPC caller redirect the object-directory search path seen by administrator-installed custom hooks to an arbitrary filesystem location, which is exactly the class of "storage escape / object-pool isolation bypass" this scan is looking for: custom hooks (and any git subprocess they spawn) could read or, depending on the hook's own git invocation flags, write objects outside the intended repository, undermining the object quarantine/alternates isolation model documented in `doc/object_quarantine.md`.

### Likelihood Explanation
I was unable to fully confirm, within the available investigation budget, whether the code paths that call `UpdateReference` with a `nil` quarantine directory (which is the precondition for this to matter) are actually reachable with a client-controlled `Repository.GitObjectDirectory` value in the current codebase's OperationService call sites, or whether some upstream validation/normalization of the `Repository` message strips these fields before they reach `UpdateReference`. I was also not able to verify whether Gitaly's gRPC field-level authorization treats `git_object_directory`/`git_alternate_object_directories` as trusted-only fields that get overwritten before hitting the operations layer. Given this residual uncertainty, I cannot assert this is a confirmed, exploitable finding — it is a genuine code-level inconsistency (missing `storage.ValidateRelativePath` call that exists everywhere else this kind of value is consumed) that warrants dedicated verification rather than a proven PoC.

### Recommendation
Regardless of the exact reachability, `customHooksEnv` should validate `payload.Repo.GetGitObjectDirectory()` / `GetGitAlternateObjectDirectories()` the same way `ObjectDirectoryPath` does (via `storage.ValidateRelativePath` plus the quarantine-prefix check) before exporting them as `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES`, rather than doing an unchecked `filepath.Join`.

### Proof of Concept
Not verified end-to-end; a confirmed PoC would require tracing an OperationService RPC path where a repository with a custom pre-receive hook installed is updated via `UpdateReference` with `quarantineDir == nil` and a `Repository.GitObjectDirectory` value supplied directly in the RPC request, then observing the resulting `GIT_OBJECT_DIRECTORY` value passed to the custom hook process.

---
**Caveat on scope**: given the amount of hardening already present elsewhere in this codebase (`storage.ValidateRelativePath` in `internal/gitaly/storage/locator.go` [7](#0-6) , alternates-outside-storage checks in `internal/git/dirs.go` [8](#0-7) , symlink/hardlink escape checks in `extractTarToDirectory` [9](#0-8) , and quarantine-prefix validation in `ObjectDirectoryPath`), the finding above in `customHooksEnv` is the one concrete gap I located that matches the requested bug class (hook/quarantine gating) but whose full exploit chain from an ordinary user's RPC I could not conclusively verify.

### Citations

**File:** proto/go/gitalypb/shared.pb.go (L253-259)
```go
	// git_object_directory sets the GIT_OBJECT_DIRECTORY envvar on git commands to the value of this field.
	// It influences the object storage directory the SHA1 directories are created underneath.
	GitObjectDirectory string `protobuf:"bytes,4,opt,name=git_object_directory,json=gitObjectDirectory,proto3" json:"git_object_directory,omitempty"`
	// git_alternate_object_directories sets the GIT_ALTERNATE_OBJECT_DIRECTORIES envvar on git commands to
	// the values of this field. It influences the list of Git object directories which can be used to search
	// for Git objects.
	GitAlternateObjectDirectories []string `protobuf:"bytes,5,rep,name=git_alternate_object_directories,json=gitAlternateObjectDirectories,proto3" json:"git_alternate_object_directories,omitempty"`
```

**File:** internal/git/gitcmd/hooks_payload.go (L159-167)
```go
	return HooksPayload{
		Repo: &gitalypb.Repository{
			StorageName:                   repo.GetStorageName(),
			RelativePath:                  repo.GetRelativePath(),
			GitObjectDirectory:            repo.GetGitObjectDirectory(),
			GitAlternateObjectDirectories: repo.GetGitAlternateObjectDirectories(),
			GlRepository:                  repo.GetGlRepository(),
			GlProjectPath:                 repo.GetGlProjectPath(),
		},
```

**File:** internal/gitaly/hook/custom.go (L172-190)
```go
	objectDirectory := env.ExtractValue(envs, "GIT_OBJECT_DIRECTORY")
	if objectDirectory == "" && payload.Repo.GetGitObjectDirectory() != "" {
		objectDirectory = filepath.Join(repoPath, payload.Repo.GetGitObjectDirectory())
	}
	if objectDirectory != "" {
		customEnvs = append(customEnvs, "GIT_OBJECT_DIRECTORY="+objectDirectory)
	}

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

**File:** internal/git/localrepo/paths.go (L27-75)
```go
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

**File:** internal/gitaly/hook/prereceive.go (L108-116)
```go
	if gitObjDir, gitAltObjDirs := env.ExtractValue(envs, "GIT_OBJECT_DIRECTORY"), env.ExtractValue(envs, "GIT_ALTERNATE_OBJECT_DIRECTORIES"); gitObjDir != "" && gitAltObjDirs != "" {
		gitObjectDirRel, gitAltObjectDirRel, err := getRelativeObjectDirs(repoPath, gitObjDir, gitAltObjDirs)
		if err != nil {
			return structerr.NewInternal("getting relative git object directories: %w", err)
		}

		repo.GitObjectDirectory = gitObjectDirRel
		repo.GitAlternateObjectDirectories = gitAltObjectDirRel
	}
```

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L208-221)
```go
	// In case there's no quarantine directory, we simply take the normal unquarantined
	// repository as input for the hooks payload. Otherwise, we'll take the quarantined
	// repository, which carries information about the quarantined object directory. This is
	// then subsequently passed to Rails, which can use the quarantine directory to more
	// efficiently query which objects are new.
	quarantinedRepo := repoProto
	if quarantineDir != nil {
		quarantinedRepo = quarantineDir.QuarantinedRepo()
	}

	hooksPayload, err := gitcmd.NewHooksPayload(ctx, u.cfg, quarantinedRepo, objectHash, transaction, &receiveHooksPayload, gitcmd.ReceivePackHooks, featureflag.FromContext(ctx), storage.ExtractTransactionID(ctx)).Env()
	if err != nil {
		return fmt.Errorf("constructing hooks payload: %w", err)
	}
```

**File:** internal/gitaly/storage/locator.go (L154-164)
```go
// ValidateRelativePath validates a relative path by joining it with rootDir and verifying the result
// is either rootDir or a path within rootDir. Returns clean relative path from rootDir to relativePath
// or an ErrRelativePathEscapesRoot if the resulting path is not contained within rootDir.
func ValidateRelativePath(rootDir, relativePath string) (string, error) {
	absPath := filepath.Join(rootDir, relativePath)
	if rootDir != absPath && !strings.HasPrefix(absPath, rootDir+string(os.PathSeparator)) {
		return "", ErrRelativePathEscapesRoot
	}

	return filepath.Rel(rootDir, absPath)
}
```

**File:** internal/git/dirs.go (L79-85)
```go
		if !filepath.IsAbs(newDir) {
			newDir = filepath.Join(objDir, newDir)
		}

		if !strings.HasPrefix(newDir, storagePrefix) {
			return nil, alternateOutsideStorageError(newDir)
		}
```

**File:** internal/gitaly/service/repository/replicate.go (L336-364)
```go
		if !strings.HasPrefix(targetPath, targetDir+string(os.PathSeparator)) &&
			targetPath != targetDir {
			return fmt.Errorf("invalid file path in tar: %s", header.Name)
		}

		switch header.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(targetPath, os.FileMode(header.Mode)); err != nil {
				return fmt.Errorf("creating directory %s: %w", targetPath, err)
			}

		case tar.TypeReg:
			if err := s.extractFile(ctx, tarReader, targetPath, header); err != nil {
				return fmt.Errorf("extracting file %s: %w", targetPath, err)
			}

		case tar.TypeSymlink:
			if filepath.IsAbs(header.Linkname) {
				return fmt.Errorf("absolute symlink not allowed: %s -> %s", header.Name, header.Linkname)
			}

			// Resolve the relative symlink target from the symlink's parent directory
			// and verify it stays within the extraction boundary, consistent with the
			// hard link validation below.
			resolvedTarget := filepath.Join(filepath.Dir(targetPath), header.Linkname)
			if !strings.HasPrefix(resolvedTarget, targetDir+string(os.PathSeparator)) &&
				resolvedTarget != targetDir {
				return fmt.Errorf("symlink target escapes extraction directory: %s -> %s", header.Name, header.Linkname)
			}
```
