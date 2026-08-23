### Title
Client-supplied `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` bypass path validation and are injected verbatim into custom hook execution environment - (File: `internal/gitaly/hook/custom.go`)

### Summary
The `HookService.PreReceiveHook`/`PostReceiveHook` RPCs accept a client-controlled `EnvironmentVariables` field that is trusted as if it originated from Git's own quarantine mechanism. `validatePreReceiveHookRequest` only validates the `Repository` field [1](#0-0) ; it never validates the contents of `EnvironmentVariables`, which are then forwarded straight into `GitLabHookManager.PreReceiveHook` → `preReceiveHook` → `customHooksEnv` [2](#0-1) .

### Finding Description
`customHooksEnv` extracts `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` from the caller-supplied `envs` slice and, if present, copies them **verbatim** into the environment handed to the repo's custom hooks, with no check that these paths are anywhere near the target repository: [3](#0-2) 

Compare this to the equivalent, but properly-guarded, logic used elsewhere in Gitaly for resolving an object directory from client-supplied data, `Repo.ObjectDirectoryPath`, which explicitly calls `storage.ValidateRelativePath` and rejects any path that escapes the repository/storage root [4](#0-3) [5](#0-4) . No equivalent bound-check exists in `customHooksEnv`.

Separately, when the `preReceiveHook` normalizes an incoming absolute `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` pair into a repo-relative path (to hand back to Rails via `gitlab.AllowedParams`), it uses `getRelativeObjectDirs`, which merely calls `filepath.Rel` and never verifies the resulting relative path stays inside the repository: [6](#0-5) 

This is the same bug class as the TOFT report: a message-processing pathway trusts a field supplied through the message (here, `PreReceiveHookRequest.EnvironmentVariables`) and uses it with the full privilege/identity of the trusted actor (here, the Gitaly server process spawning the custom hook with server-chosen `GIT_DIR`/`PATH`/`GL_*` values) without revalidating that the field's content is consistent with what a legitimately-spawned `git receive-pack` subprocess would have produced.

### Impact Explanation
If `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` supplied in the RPC point outside the repository (e.g., to another tenant's `.git/objects` directory or an arbitrary path on the storage), the admin-configured custom pre-receive/post-receive hook — which runs with Gitaly's own privileges via `command.New` [7](#0-6)  — will search for objects in that attacker-chosen directory. Any git subcommand the custom hook invokes (`git cat-file`, `git log`, etc., a common pattern per the shipped hooks tests [8](#0-7) ) would then resolve blobs/commits from that foreign object directory, enabling cross-repository object disclosure. The unvalidated relative path is also forwarded to Rails as `GitObjectDirectory`/`GitAlternateObjectDirectories` in `gitlab.AllowedParams` [9](#0-8) , which per `doc/object_quarantine.md` gets echoed back into subsequent Gitaly RPCs during the same Rails request [10](#0-9)  — though those downstream RPCs (e.g. `GetObjectDirectorySize`) do apply `ValidateRelativePath`-style checks and would reject an escaping path, limiting the practical blast radius to the custom-hook execution environment itself.

### Likelihood Explanation
Reachability depends on whether `HookService.PreReceiveHook`/`PostReceiveHook` can be invoked with an attacker-crafted `EnvironmentVariables` field independent of an actual `git receive-pack` invocation. In the normal flow, `gitaly-hooks` populates this field from `os.Environ()` of the hook subprocess spawned by Git itself [11](#0-10) , which is set by Gitaly's own quarantine setup rather than by a remote pusher directly. Reaching this bug therefore likely requires the ability to submit a crafted `PreReceiveHookRequest` directly over gRPC (an internal/authenticated RPC surface) rather than through an ordinary `git push`; I could not fully verify from the indexed code whether any unprivileged/external caller can reach `HookService` with attacker-chosen `EnvironmentVariables` without going through the intended `gitaly-hooks` binary, so likelihood is assessed as moderate/uncertain rather than confirmed high.

### Recommendation
- In `customHooksEnv` (`internal/gitaly/hook/custom.go`), validate any `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES` values extracted from `envs` using `storage.ValidateRelativePath` (or equivalent) against the repository/storage root before injecting them into the custom hook's environment, mirroring the logic in `Repo.ObjectDirectoryPath`.
- In `getRelativeObjectDirs` (`internal/gitaly/hook/prereceive.go`), reject any object directory whose `filepath.Rel` result starts with `..` (escapes `repoPathReal`) instead of accepting it unconditionally.
- Confirm and, if necessary, restrict which callers are authorized to invoke `HookService.PreReceiveHook`/`PostReceiveHook` with client-supplied `EnvironmentVariables`, since this field is currently treated as fully trusted.

### Proof of Concept
Reachability of `PreReceiveHookRequest.EnvironmentVariables` from an unprivileged external caller (versus only from the `gitaly-hooks` binary invoked by `git receive-pack`) could not be conclusively confirmed with the available index; a full PoC would require tracing gRPC authorization/exposure of `HookService` end-to-end (e.g., via Praefect routing and gitlab-shell), which is not fully covered by the indexed files. Given this uncertainty about attacker reachability, this should be treated as a **defense-in-depth gap** (missing validation compared to the pattern used elsewhere in the codebase) rather than a confirmed remotely-exploitable vulnerability, pending verification with the full source in a Devin session.

### Citations

**File:** internal/gitaly/service/hook/pre_receive.go (L41-49)
```go
	if err := s.manager.PreReceiveHook(
		stream.Context(),
		repository,
		firstRequest.GetGitPushOptions(),
		firstRequest.GetEnvironmentVariables(),
		stdin,
		stdout,
		stderr,
	); err != nil {
```

**File:** internal/gitaly/service/hook/pre_receive.go (L67-69)
```go
func validatePreReceiveHookRequest(ctx context.Context, locator storage.Locator, in *gitalypb.PreReceiveHookRequest) error {
	return locator.ValidateRepository(ctx, in.GetRepository())
}
```

**File:** internal/gitaly/hook/custom.go (L86-97)
```go
		for _, hookFile := range hookFiles {
			c, err := command.New(ctx, m.logger, append([]string{hookFile}, args...),
				command.WithDir(repoPath),
				command.WithStdin(bytes.NewReader(stdinBytes)),
				command.WithStdout(stdout),
				command.WithStderr(stderr),
				command.WithEnvironment(env),
				command.WithCommandName("gitaly-hooks", hookName),
			)
			if err != nil {
				return err
			}
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

**File:** internal/git/localrepo/paths.go (L36-41)
```go

	// Ensure the path points somewhere in the storage.
	relativeObjectDirectoryPath, err := storage.ValidateRelativePath(storagePath, filepath.Join(repoPath, objectDirectoryPath))
	if err != nil {
		return "", structerr.NewInvalidArgument("validate relative path: %w", err)
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

**File:** internal/gitaly/hook/prereceive.go (L41-63)
```go
func getRelativeObjectDirs(repoPath, gitObjectDir, gitAlternateObjectDirs string) (string, []string, error) {
	repoPathReal, err := filepath.EvalSymlinks(repoPath)
	if err != nil {
		return "", nil, err
	}

	gitObjDirRel, err := filepath.Rel(repoPathReal, gitObjectDir)
	if err != nil {
		return "", nil, err
	}

	var gitAltObjDirsRel []string

	for _, gitAltObjDirAbs := range strings.Split(gitAlternateObjectDirs, ":") {
		gitAltObjDirRel, err := filepath.Rel(repoPathReal, gitAltObjDirAbs)
		if err != nil {
			return "", nil, err
		}

		gitAltObjDirsRel = append(gitAltObjDirsRel, gitAltObjDirRel)
	}

	return gitObjDirRel, gitAltObjDirsRel, nil
```

**File:** internal/gitaly/hook/prereceive.go (L135-146)
```go
	params := gitlab.AllowedParams{
		RepoPath:                      repoPath,
		RelativePath:                  repo.GetRelativePath(),
		GitObjectDirectory:            repo.GetGitObjectDirectory(),
		GitAlternateObjectDirectories: repo.GetGitAlternateObjectDirectories(),
		GLRepository:                  repo.GetGlRepository(),
		GLID:                          payload.UserDetails.UserID,
		GLProtocol:                    payload.UserDetails.Protocol,
		Changes:                       string(changes),
		PushOptions:                   pushOptions,
		ClientContext:                 payload.GitalyClientContext,
	}
```

**File:** internal/gitaly/hook/postreceive_test.go (L591-594)
```go
	gittest.WriteCustomHook(t, repoPath, "post-receive", []byte(fmt.Sprintf(
		`#!/bin/sh
		git cat-file -p %q || true
	`, blobID.String())))
```

**File:** doc/object_quarantine.md (L109-119)
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
```

**File:** cmd/gitaly-hooks/hooks.go (L301-313)
```go
func preReceiveHook(ctx context.Context, payload gitcmd.HooksPayload, hookClient gitalypb.HookServiceClient, args []string) error {
	preReceiveHookStream, err := hookClient.PreReceiveHook(ctx)
	if err != nil {
		return fmt.Errorf("error when getting preReceiveHookStream client for: %w", err)
	}

	if err := preReceiveHookStream.Send(&gitalypb.PreReceiveHookRequest{
		Repository:           payload.Repo,
		EnvironmentVariables: os.Environ(),
		GitPushOptions:       gitPushOptions(),
	}); err != nil {
		return fmt.Errorf("error when sending request for pre-receive hook: %w", err)
	}
```
