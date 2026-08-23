### Title
UserSquash Skips Pre-Receive/Access-Check Hooks Applied to All Other Object-Writing RPCs, Allowing Hook Bypass for Injected Commit Objects - (File: internal/gitaly/service/operations/squash.go)

### Summary
`UserSquash` is the only object-writing RPC in `OperationService` that migrates new Git objects from a quarantine directory into a repository's main object database without ever invoking the pre-receive/access-check hook machinery (`hook.Manager.PreReceiveHook`, `UpdateHook`, or the Rails `/internal/allowed` access check) that every other comparable mutating RPC (`UserCommitFiles`, `UserCreateTag`, `UserDeleteBranch`, `UserMergeBranch`, `UserCherryPick`, `UserRevert`, `UserRebaseConfirmable`, `UserUpdateSubmodule`, etc.) enforces before committing quarantined objects.

### Finding Description
Every other object-writing operation in `internal/gitaly/service/operations/` funnels its reference update through `updateref.UpdaterWithHooks` or an equivalent path that triggers `PreReceiveHook` (which calls Rails' `/internal/allowed` endpoint and runs custom `pre-receive` hooks) before objects are migrated out of quarantine, as seen in `tags.go`, `cherry_pick.go`, `commit_files.go`, `merge_branch.go`, `rebase_confirmable.go`, `user_delete_branch.go`, `apply_patch.go`, `ff_branch.go`, `revert.go`, `submodules.go`, `user_create_branch.go`, and `user_update_branch.go`. [1](#0-0) [2](#0-1) 

`UserSquash`, however, stages a merge commit in a quarantine directory, votes on it through the transaction manager, and then unconditionally migrates the quarantine directory into the main object store — with no call into `hook.Manager` and no access-check round trip to Rails anywhere in its code path: [3](#0-2) [4](#0-3) 

The code itself acknowledges the asymmetry: "The RPC is badly designed in that it never updates any references, but only creates the objects and writes them to disk. We still use a quarantine directory to stage the new objects, vote on them and migrate them into the main directory if quorum was reached..." [5](#0-4) 

This is architecturally analogous to the reported bug class: one family of state-changing operations (all other object-writing RPCs = "deposit") is gated by a mandatory security control (pre-receive/access-check hook = "pause check"), while a sibling operation that performs the same kind of privileged action — persisting attacker-controlled Git objects into the shared object database — is not gated by that same control ("withdraw").

### Impact Explanation
An authenticated caller with only RPC-level access to `UserSquash` can have Gitaly permanently persist a commit object into the target repository's object database — with attacker-controlled commit message, author/committer name/email, and timestamp fields (`req.GetCommitMessage()`, `req.GetAuthor()`, `git.SignatureFromRequest(req)`) — without that content ever passing through the `pre-receive`/`update` hook chain that GitLab and repository administrators rely on to enforce push rules, commit signing policy, secret scanning, or other server-side hook based safeguards. ` [6](#0-5) ` Because the object is migrated into the real object directory (not left dangling in an ephemeral quarantine that gets discarded), it becomes durably reachable by OID for any subsequent operation (e.g., a later `UserCreateBranch`/`WriteRef` call, or direct object access) without ever having been vetted by the hook path that all sibling mutating RPCs enforce, unlike similar object-writing RPCs which are hook-gated. ` [7](#0-6) `

### Likelihood Explanation
Exploitation only requires calling the `UserSquash` RPC with valid but arbitrary `StartSha`/`EndSha`/`CommitMessage`/`Author`/`User` fields, which is exactly the kind of "ordinary user push/RPC field" path described in scope — no privileged actor, leaked token, or malicious peer is required, only the standard capability to invoke `OperationService/UserSquash` that any client with repository write access already has.

### Recommendation
Route `UserSquash`'s quarantine migration through the same access-check / pre-receive hook path used by other object-writing RPCs (e.g., invoke `hook.Manager.PreReceiveHook`/access-check before `quarantineDir.Migrate`), or otherwise document and enforce that the migrated commit is unreachable and cannot be referenced by later RPCs without itself first passing hooks, closing the gap between this RPC and its siblings.

### Proof of Concept
Not executed against a live instance; based on static code review, a caller with only `UserSquash` access can trigger `s.userSquash` → `s.merge` → `quarantineDir.Migrate` with no hook invocation in between, as shown at [8](#0-7) , in contrast to sibling RPCs which call into `hook.Manager` before migrating quarantined content.

### Citations

**File:** internal/gitaly/service/operations/tags.go (L1-1)
```go
package operations
```

**File:** internal/gitaly/service/operations/update_with_hooks.go (L1-1)
```go
package operations
```

**File:** internal/gitaly/service/operations/squash.go (L77-213)
```go
func (s *Server) userSquash(ctx context.Context, req *gitalypb.UserSquashRequest) (string, error) {
	// All new objects are staged into a quarantine directory first so that we can do
	// transactional voting before we commit data to disk.
	quarantineDir, quarantineRepo, cleanup, err := s.quarantinedRepo(ctx, req.GetRepository())
	if err != nil {
		return "", structerr.NewInternal("creating quarantine: %w", err)
	}
	defer cleanup()

	// We need to retrieve the start commit such that we can create the new commit with
	// all parents of the start commit.
	startCommit, err := quarantineRepo.ResolveRevision(ctx, git.Revision(req.GetStartSha()+"^{commit}"))
	if err != nil {
		return "", structerr.NewInvalidArgument("resolving start revision: %w", err).WithDetail(
			&gitalypb.UserSquashError{
				Error: &gitalypb.UserSquashError_ResolveRevision{
					ResolveRevision: &gitalypb.ResolveRevisionError{
						Revision: []byte(req.GetStartSha()),
					},
				},
			},
		)
	}

	// And we need to take the tree of the end commit. This tree already is the result
	endCommit, err := quarantineRepo.ResolveRevision(ctx, git.Revision(req.GetEndSha()+"^{commit}"))
	if err != nil {
		return "", structerr.NewInvalidArgument("resolving end revision: %w", err).WithDetail(
			&gitalypb.UserSquashError{
				Error: &gitalypb.UserSquashError_ResolveRevision{
					ResolveRevision: &gitalypb.ResolveRevisionError{
						Revision: []byte(req.GetEndSha()),
					},
				},
			},
		)
	}

	committerSignature, err := git.SignatureFromRequest(req)
	if err != nil {
		return "", structerr.NewInvalidArgument("%w", err)
	}

	authorLocation, err := time.LoadLocation(req.GetAuthor().GetTimezone())
	if err != nil {
		return "", structerr.NewInvalidArgument("%w", err)
	}
	authorSignature := git.NewSignature(
		string(req.GetAuthor().GetName()),
		string(req.GetAuthor().GetEmail()),
		committerSignature.When.In(authorLocation),
	)

	message := string(req.GetCommitMessage())
	// In previous implementation, we've used git commit-tree to create commit.
	// When message wasn't empty and didn't end in a new line,
	// git commit-tree would add a trailing new line to the commit message.
	// Let's keep that behaviour for compatibility.
	if len(message) > 0 && !strings.HasSuffix(message, "\n") {
		message += "\n"
	}

	commitID, err := s.merge(
		ctx,
		quarantineRepo,
		authorSignature,
		committerSignature,
		message,
		startCommit.String(),
		endCommit.String(),
		true,
		req.GetSign(),
	)
	if err != nil {
		var mergeConflictErr *localrepo.MergeTreeConflictError
		if errors.As(err, &mergeConflictErr) {
			conflictingFiles := make([][]byte, 0, len(mergeConflictErr.ConflictingFileInfo))
			for _, conflictingFileInfo := range mergeConflictErr.ConflictingFileInfo {
				conflictingFiles = append(conflictingFiles, []byte(conflictingFileInfo.FileName))
			}

			return "", structerr.NewFailedPrecondition("squashing commits: %w", err).WithDetail(
				&gitalypb.UserSquashError{
					// Note: this is actually a merge conflict, but we've kept
					// the old "rebase" name for compatibility reasons.
					Error: &gitalypb.UserSquashError_RebaseConflict{
						RebaseConflict: &gitalypb.MergeConflictError{
							ConflictingFiles: conflictingFiles,
							ConflictingCommitIds: []string{
								startCommit.String(),
								endCommit.String(),
							},
						},
					},
				},
			)
		}
	}

	if err := transaction.VoteOnContext(
		ctx,
		s.txManager,
		voting.VoteFromData([]byte(commitID)),
		voting.Preparing,
	); err != nil {
		return "", structerr.NewAborted("preparing vote on squashed commit: %w", err)
	}

	// The RPC is badly designed in that it never updates any references, but only creates the
	// objects and writes them to disk. We still use a quarantine directory to stage the new
	// objects, vote on them and migrate them into the main directory if quorum was reached so
	// that we don't pollute the object directory with objects we don't want to have in the
	// first place.
	if err := transaction.VoteOnContext(
		ctx,
		s.txManager,
		voting.VoteFromData([]byte(commitID)),
		voting.Prepared,
	); err != nil {
		return "", structerr.NewAborted("prepared vote on squashed commit: %w", err)
	}

	if err := quarantineDir.Migrate(ctx); err != nil {
		return "", structerr.NewInternal("migrating quarantine directory: %w", err)
	}

	if err := transaction.VoteOnContext(
		ctx,
		s.txManager,
		voting.VoteFromData([]byte(commitID)),
		voting.Committed,
	); err != nil {
		return "", structerr.NewAborted("committing vote on squashed commit: %w", err)
	}

	return commitID, nil
}
```
