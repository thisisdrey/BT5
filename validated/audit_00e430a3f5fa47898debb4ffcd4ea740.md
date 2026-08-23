### Title
UserApplyPatch writes patch commits directly into the live object database, bypassing the object quarantine used by every other write RPC - (File: internal/gitaly/service/operations/apply_patch.go)

### Summary
`UserApplyPatch` builds a plain, unquarantined repository handle and writes the patched commits into it before running any pre-receive/access checks, whereas every sibling write-RPC in `OperationService` (`UserCommitFiles`, `UserMergeBranch`, `UserCherryPick`, `UserRevert`, `UserCreateTag`, `UserRebaseConfirmable`, `UserUpdateSubmodule`) first creates an object quarantine via `s.quarantinedRepo()` and only migrates the objects into the main repository after `PreReceiveHook`/`Allowed()` succeeds.

### Finding Description
In `internal/gitaly/service/operations/apply_patch.go`, `userApplyPatch()` does: [1](#0-0) 
and then commits the parsed mailbox patches straight into that repository via `applyPatchesWithIndex`/`repo.WriteCommit`: [2](#0-1) 
Only afterwards is `updateReferenceWithHooks` invoked, and critically it is called with a `nil` quarantine directory: [3](#0-2) 

Compare this to every other mutating RPC in the same package, which first obtains a quarantine directory and quarantined repo handle with `s.quarantinedRepo(ctx, ...)`, performs all object writes inside that quarantine, and only passes the non-nil `quarantineDir` into `updateReferenceWithHooks`, e.g. `UserUpdateSubmodule`: [4](#0-3) [5](#0-4) 
and the shared quarantine helper: [6](#0-5) 

The quarantine mechanism exists precisely so that objects created by a write RPC are kept detached from the "real" object database until the GitLab `/internal/allowed` access check (which push rules, signature verification, and other pre-receive checks hook into) has approved the change, and are only migrated in afterward: [7](#0-6) 
`doc/hooks.md` documents this as the intended isolation model — objects live in "a separate 'quarantine directory'... detached from the main repository" until access checks succeed: [8](#0-7) 

Because `UserApplyPatch` skips this step, the commit/tree/blob objects produced from the attacker-supplied patch mailbox are written as loose objects directly into the target repository's real object directory *before* `PreReceiveHook` calls Rails' `Allowed()` (the endpoint that performs push-rule/commit validation): [9](#0-8) 

### Impact Explanation
This is the Gitaly-level analog of the reported bug class: content that has not yet passed the push-rule/access-check gate becomes durably present in the repository's real object store rather than being confined to a disposable quarantine directory. Concretely:
- If the Rails `/allowed` check rejects the patch (e.g., due to a push rule violation, unsigned-commit requirement, blocked file names, etc.), the corresponding commit/tree/blob objects have *already* been written into the live repository as loose objects and are not rolled back — only the ref update is skipped. They remain fetchable by object ID (`GetBlob`, `CatFile`, etc.) by any actor with read access to the repository, and will only disappear once a future `git gc`/pruning cycle reaps them.
- This breaks the intended "commit content is invisible until the access checks pass" invariant that quarantine directories exist to enforce for every other write path, and is a direct structural deviation from the pattern used by `UserCommitFiles`, `UserMergeBranch`, `UserCherryPick`, `UserRevert`, `UserCreateTag`, `UserRebaseConfirmable`, and `UserUpdateSubmodule`.
- It also means any custom repository-object-directory-based inspection Rails performs against the quarantine (`GitObjectDirectory`/`GitAlternateObjectDirectories`) is moot for this RPC — those fields end up empty since there is no quarantine repo, so Rails/GitLab-side push-rule checks that specifically rely on being pointed at an isolated quarantine object dir operate on a different assumption than for a normal git push, increasing the chance that quarantine-dependent validation logic diverges in behavior for `UserApplyPatch`-created commits versus normal receive-pack pushes.

### Likelihood Explanation
`UserApplyPatch` is a `MUTATOR` RPC reachable by any user who can create merge requests / apply patches through GitLab's "create MR by email" or patch-apply feature — exactly the same feature described in the original report — and requires no special privilege beyond normal repository write access. No malicious peer, leaked token, or admin privilege is required; a single crafted email/patch stream to the RPC is sufficient to trigger the code path.

### Recommendation
Make `UserApplyPatch` consistent with the rest of the `OperationService` write paths: acquire a quarantine directory with `s.quarantinedRepo(ctx, header.GetRepository())`, perform `applyPatchesWithIndex`/`repo.WriteCommit` against the quarantined repo handle, and pass the resulting non-nil `quarantineDir` into `updateReferenceWithHooks` so that patch-derived objects are only migrated into the real object database after `PreReceiveHook`/`Allowed()` succeeds.

### Proof of Concept
1. Configure a project with strict push rules (e.g. `Reject unsigned commits`, commit message regex, prohibited file names) as in the original report.
2. Call the Gitaly `UserApplyPatch` streaming RPC directly (as GitLab's email-to-MR worker does) with a header targeting a new/existing branch and a patch mailbox attachment containing a commit that violates the configured push rules.
3. Observe that regardless of whether the subsequent `PreReceiveHook`/`Allowed()` call rejects the ref update (`denied by custom hooks` / access-check error), the commit/tree/blob objects for the patch have already been written as loose objects into the target repository's real object directory (verifiable by inspecting `objects/` in the repo path immediately after the RPC returns an error) — unlike the same failure scenario for `UserCommitFiles`/`UserCherryPick`/etc., where rejected objects only ever exist in the (now-discarded) quarantine directory and never touch the real object database.

### Citations

**File:** internal/gitaly/service/operations/apply_patch.go (L62-67)
```go
func (s *Server) userApplyPatch(ctx context.Context, header *gitalypb.UserApplyPatchRequest_Header, stream gitalypb.OperationService_UserApplyPatchServer) (returnedErr error) {
	branchCreated := false
	targetBranch := git.NewReferenceNameFromBranchName(string(header.GetTargetBranch()))

	repo := s.localRepoFactory.Build(header.GetRepository())

```

**File:** internal/gitaly/service/operations/apply_patch.go (L128-130)
```go
	if err := s.updateReferenceWithHooks(ctx, header.GetRepository(), header.GetUser(), nil, targetBranch, patchedCommit, currentCommit); err != nil {
		return fmt.Errorf("update reference: %w", err)
	}
```

**File:** internal/gitaly/service/operations/apply_patch.go (L203-219)
```go
		commitID, err := repo.WriteCommit(ctx, localrepo.WriteCommitConfig{
			Parents:        []git.ObjectID{currentCommitID},
			TreeID:         treeID,
			AuthorName:     patch.authorName,
			AuthorEmail:    patch.authorEmail,
			AuthorDate:     authorDate,
			CommitterName:  committerSignature.Name,
			CommitterEmail: committerSignature.Email,
			CommitterDate:  committerSignature.When,
			Message:        commitMessage,
		})
		if err != nil {
			return "", fmt.Errorf("committing patch %d: %w", i+1, err)
		}

		currentCommitID = commitID
	}
```

**File:** internal/gitaly/service/operations/submodules.go (L28-32)
```go
	quarantineDir, quarantineRepo, cleanup, err := s.quarantinedRepo(ctx, req.GetRepository())
	if err != nil {
		return nil, err
	}
	defer cleanup()
```

**File:** internal/gitaly/service/operations/submodules.go (L120-128)
```go
	if err := s.updateReferenceWithHooks(
		ctx,
		req.GetRepository(),
		req.GetUser(),
		quarantineDir,
		referenceName,
		commitOID,
		oldOID,
	); err != nil {
```

**File:** internal/gitaly/service/operations/server.go (L50-58)
```go
func (s *Server) quarantinedRepo(ctx context.Context, repo *gitalypb.Repository) (*quarantine.Dir, *localrepo.Repo, func(), error) {
	quarantineDir, cleanup, err := quarantine.New(ctx, repo, s.logger, s.locator)
	if err != nil {
		return nil, nil, nil, structerr.NewInternal("creating object quarantine: %w", err)
	}

	quarantineRepo := s.localRepoFactory.Build(quarantineDir.QuarantinedRepo())
	return quarantineDir, quarantineRepo, cleanup, nil
}
```

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L200-235)
```go
	changes := fmt.Sprintf("%s %s %s\n", oldrev, newrev, reference)

	receiveHooksPayload := gitcmd.UserDetails{
		UserID:   user.GetGlId(),
		Username: user.GetGlUsername(),
		Protocol: "web",
	}

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

	var stdout, stderr bytes.Buffer
	if err := u.hookManager.PreReceiveHook(ctx, quarantinedRepo, pushOptions, []string{hooksPayload}, strings.NewReader(changes), &stdout, &stderr); err != nil {
		return fmt.Errorf("running pre-receive hooks: %w", wrapHookError(err, gitcmd.PreReceiveHook, stdout.String(), stderr.String()))
	}

	// Now that Rails has told us that the change is okay via the pre-receive hook, we can
	// migrate any potentially quarantined objects into the main repository. This must happen
	// before we start updating the refs because git-update-ref(1) will verify that it got all
	// referenced objects available.
	if quarantineDir != nil {
		if err := quarantineDir.Migrate(ctx); err != nil {
			return fmt.Errorf("migrating quarantined objects: %w", err)
		}
```

**File:** doc/hooks.md (L205-220)
```markdown
These hooks perform the following functions:

- `pre-receive`: The pre-receive hook receives all reference updates as a whole
  via standard input, where each change is represented by one line with the old
  and new object ID as well the name of the reference that is to be updated. At
  this point, all objects required to satisfy the update have already been
  received, but they are still in a separate "quarantine directory" and are
  therefore detached from the main repository. This hook first increments a
  reference counter that tracks how many pushes are active at the same time.
  Afterwards, it posts all changes to Rails' `/internal/allowed` API endpoint so
  that Rails can determine whether the change is allowed or not. Because objects
  still live in a quarantine directory, Gitaly tells Rails where it can find the
  quarantine directory using the repository's alternative object directory
  fields so that any subsequent RPC calls that check the change can access those
  objects. When the access checks succeed, any existing custom pre-receive hooks
  installed by the administrator are executed.
```

**File:** internal/gitaly/hook/prereceive.go (L135-163)
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

	allowed, message, err := m.gitlabClient.Allowed(ctx, params)
	if err != nil {
		// This logic is broken because we just return every potential error to the
		// caller, even though we cannot tell whether the error message stems from
		// the API or if it is a generic error. Ideally, we'd be able to tell
		// whether the error was a PermissionDenied error and only then return
		// the error message as GitLab message. But this will require upstream
		// changes in gitlab-shell first.
		return NotAllowedError{
			Message:  err.Error(),
			UserID:   payload.UserDetails.UserID,
			Protocol: payload.UserDetails.Protocol,
			Changes:  changes,
			cause:    err,
		}
	}
```
