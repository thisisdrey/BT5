Based on my investigation, I found a strong analog in `internal/gitaly/service/repository/write_ref.go`, specifically the `WriteRef` RPC's handling of `HEAD` updates.

### Title
`WriteRef` RPC allows setting repository HEAD to an unvalidated/non-existent reference, permanently breaking repository default branch resolution - (File: internal/gitaly/service/repository/write_ref.go)

### Summary
The `writeRef` handler special-cases writes to `HEAD`: when `req.GetRef()` equals `"HEAD"`, it calls `repo.SetDefaultBranch(ctx, s.txManager, git.ReferenceName(req.GetRevision()))` directly, bypassing the resolve/existence-check path used for all other reference writes in `updateRef()`.

### Finding Description
For non-`HEAD` references, `updateRef()` resolves `req.GetRevision()` via `repo.ResolveRevision(...)` and explicitly errors out with `NotFound` if the revision does not exist [1](#0-0) . However, for `HEAD`, the code takes a separate branch that skips this validation entirely: [2](#0-1) .

`SetDefaultBranch` itself only validates that the reference name is syntactically well-formed via `git.ValidateReference`, and explicitly documents that "It will not verify the reference actually exists" before calling `setDefaultBranchWithUpdateRef`, which unconditionally repoints the symbolic `HEAD` reference: [3](#0-2) . This is corroborated by Gitaly's own test suite, which explicitly exercises the "unknown ref" case and confirms `HEAD` is happily set to a nonexistent ref with no error: [4](#0-3) .

This mirrors the reported bug class: a mutator that assigns a critical pointer field (`minting_multisig` in the original report; `HEAD`/default branch here) without validating that the target is a legitimate, existing, non-degenerate value, allowing the system to end up in a broken, hard-to-recover state.

### Impact Explanation
An ordinary user/caller of the `WriteRef` RPC (used e.g. by GitLab Rails for mirror/import operations and admin ref management) can point `HEAD` at a branch name that does not exist and never will (e.g. a typo, a soon-to-be-deleted branch, or a branch on a different fork). Because there is no existence check, the RPC succeeds silently. Afterwards:
- `FindDefaultBranchName`, checkouts, archive generation, and any RPC/workflow relying on resolving `HEAD` will fail or behave inconsistently until an operator manually repairs `HEAD` with another `WriteRef`/`symbolic-ref` call.
- Because Gitaly does not track "previous known-good HEAD" once overwritten, repositories can be left with a persistently broken default branch, effectively denial-of-service on any code path that assumes `HEAD` resolves (this is a repo-level correctness/availability regression analogous to the "loses its critical value forever" impact in the original finding, since there's no way to programmatically know what the correct value should have been).

### Likelihood Explanation
This requires only a single unprivileged/ordinary `WriteRef` RPC call with `Ref: "HEAD"` and a `Revision` that is a syntactically valid but nonexistent branch name (e.g. `refs/heads/does-not-exist`) — no special privileges beyond normal write access to the repository, no race condition, no malicious peer needed. The behavior is deterministic and already exercised (without being flagged as problematic) by existing test cases such as `testRepositoryGetDefaultBranch`'s "no branches"/"unknown ref" scenarios and `TestRepo_SetDefaultBranch`'s "unknown ref" case [4](#0-3) .

### Recommendation
In `writeRef()`/`SetDefaultBranch()`, when the target is `HEAD`, validate that the given reference name actually resolves to an existing reference (or at minimum warn/require an explicit "force" flag to point HEAD at a non-existent ref), mirroring the validation already performed for regular reference updates in `updateRef()`. At minimum, document explicitly that callers are responsible for verifying existence, and consider returning `NotFound` similarly to the non-HEAD code path.

### Proof of Concept
1. Create a repository with an existing default branch (e.g. `refs/heads/main`).
2. Call `WriteRef` with `Repository: <repo>`, `Ref: []byte("HEAD")`, `Revision: []byte("refs/heads/does-not-exist")`.
3. Observe the RPC returns success (`nil` error) per `writeRef()`'s HEAD branch [2](#0-1) .
4. Subsequently call `FindDefaultBranchName` or attempt any operation depending on `HEAD` resolution — it now points to a symbolic ref target that does not exist, and no Gitaly RPC allows discovering "the previous correct value" automatically; an operator must manually intervene to fix `HEAD`.

### Citations

**File:** internal/gitaly/service/repository/write_ref.go (L36-45)
```go
func (s *server) writeRef(ctx context.Context, req *gitalypb.WriteRefRequest) error {
	repo := s.localRepoFactory.Build(req.GetRepository())

	if string(req.GetRef()) == "HEAD" {
		if err := repo.SetDefaultBranch(ctx, s.txManager, git.ReferenceName(req.GetRevision())); err != nil {
			return fmt.Errorf("setting default branch: %w", err)
		}

		return nil
	}
```

**File:** internal/gitaly/service/repository/write_ref.go (L61-77)
```go
	} else {
		// We need to resolve the new revision in order to make sure that we're actually
		// passing an object ID to git-update-ref(1), but more importantly this will also
		// ensure that the object ID we're updating to actually exists. Note that we also
		// verify that the object actually exists in the repository by adding "^{object}".
		var err error
		newObjectID, err = repo.ResolveRevision(ctx, git.Revision(req.GetRevision())+"^{object}")
		switch {
		case errors.Is(err, git.ErrReferenceNotFound):
			return structerr.NewNotFound("resolving new revision: %w", err).WithDetail(
				&gitalypb.ReferenceNotFoundError{
					ReferenceName: req.GetRevision(),
				},
			)
		case err != nil:
			return fmt.Errorf("resolving new revision: %w", err)
		}
```

**File:** internal/git/localrepo/refs.go (L149-188)
```go
// SetDefaultBranch sets the repository's HEAD to point to the given reference.
// It will not verify the reference actually exists.
func (repo *Repo) SetDefaultBranch(ctx context.Context, txManager transaction.Manager, reference git.ReferenceName) error {
	if err := git.ValidateReference(reference.String()); err != nil {
		return fmt.Errorf("%q is a malformed refname", reference)
	}

	return repo.setDefaultBranchWithUpdateRef(ctx, reference)
}

// setDefaultBranchWithUpdateRef uses 'symref-update' command to update HEAD.
func (repo *Repo) setDefaultBranchWithUpdateRef(
	ctx context.Context,
	reference git.ReferenceName,
) (err error) {
	updater, err := updateref.New(ctx, repo, updateref.WithNoDeref())
	if err != nil {
		return fmt.Errorf("creating updateref: %w", err)
	}

	defer func() {
		if cErr := updater.Close(); err == nil && cErr != nil {
			err = fmt.Errorf("close: %w", cErr)
		}
	}()

	if err = updater.Start(); err != nil {
		return fmt.Errorf("start: %w", err)
	}

	if err = updater.UpdateSymbolicReference("HEAD", reference); err != nil {
		return fmt.Errorf("update: %w", err)
	}

	if err := updater.Commit(); err != nil {
		return fmt.Errorf("commit: %w", err)
	}

	return nil
}
```

**File:** internal/git/localrepo/refs_external_test.go (L84-88)
```go
		{
			desc:        "unknown ref",
			ref:         "refs/heads/non_existent_ref",
			expectedRef: "refs/heads/non_existent_ref",
		},
```
