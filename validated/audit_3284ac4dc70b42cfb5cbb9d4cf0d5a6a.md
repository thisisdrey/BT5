### Title
`WriteRef` allows setting repository HEAD to a non-existent reference, permanently orphaning the default branch pointer - ([File: internal/git/localrepo/refs.go])

### Summary
The `WriteRef` RPC special-cases updates to `HEAD` by calling `SetDefaultBranch`, which — unlike the regular ref-update path — never verifies that the target reference exists before committing the symbolic-ref update.

### Finding Description
In `internal/gitaly/service/repository/write_ref.go`, `writeRef` branches on whether the requested ref is `HEAD`: [1](#0-0) 

For any non-`HEAD` ref, `updateRef` resolves the target revision via `repo.ResolveRevision(..., "^{object}")`, which fails with `NotFound`/`ReferenceNotFoundError` if the target doesn't exist: [2](#0-1) 

But the `HEAD` branch instead calls `repo.SetDefaultBranch`, which explicitly documents that it "will not verify the reference actually exists" and only checks that the ref name is syntactically well-formed via `git.ValidateReference`: [3](#0-2) 

It then unconditionally writes the symbolic reference: [4](#0-3) 

`validateWriteRefRequest` also does not add this existence check — it only validates ref/revision syntax: [5](#0-4) 

As a result, an ordinary caller of `WriteRef` (or internal callers that proxy user-driven requests, e.g. `remoteRepository.SetHeadReference` used during replication/backup restore) can set `HEAD` to point at `refs/heads/does-not-exist`: [6](#0-5) 

### Impact Explanation
Setting HEAD to a dangling/non-existent branch reference breaks the repository's default-branch resolution. `GetDefaultBranch` falls back to scanning for other reference candidates only if HEAD itself doesn't resolve one of the known defaults, and other Gitaly/GitLab-Rails logic that trusts HEAD to reflect a valid default branch can be left pointing at a permanently broken symbolic reference until an operator issues another corrective `WriteRef` call. This is analogous to the reported bug class ("a position/pointer is redirected without validating the target, causing it to become unreachable/lost") — here the "position" is the repository's default branch pointer (HEAD), silently corrupted with no existence check, unlike every other ref-update code path in the same RPC which does perform this check.

### Likelihood Explanation
`WriteRef` is a standard MUTATOR RPC reachable by any client authorized to call gitaly (including internal replication/backup flows and any caller with access to the repository), and the only guard is a regex-style syntax check (`git.ValidateReference`), not existence. Triggering the bug requires no privileged access beyond ordinary repository write access — simply sending `Ref: "HEAD"`, `Revision: "refs/heads/nonexistent"`.

### Recommendation
Make `SetDefaultBranch` (or the `WriteRef` handler for the `HEAD` case) verify that the target reference exists (e.g., via `ResolveRevision`/`HasRevision`) before committing the symbolic-ref update, mirroring the validation already performed for non-HEAD reference updates in `updateRef`.

### Proof of Concept
1. Create a repository with a default branch, e.g. `refs/heads/main`.
2. Call `WriteRef` with `Repository: <repo>`, `Ref: []byte("HEAD")`, `Revision: []byte("refs/heads/does-not-exist")`.
3. Observe the RPC succeeds (no `NotFound`/`ReferenceNotFoundError`), and `HEAD` is now a symbolic reference pointing at `refs/heads/does-not-exist`, which never existed — confirmable by inspecting `TestWriteRef`'s "update default branch" test case, which shows the HEAD-update path performs no resolution check unlike the other test cases ("revision refers to missing reference") that exercise the resolving path for non-HEAD refs: [7](#0-6) [8](#0-7)

### Citations

**File:** internal/gitaly/service/repository/write_ref.go (L36-48)
```go
func (s *server) writeRef(ctx context.Context, req *gitalypb.WriteRefRequest) error {
	repo := s.localRepoFactory.Build(req.GetRepository())

	if string(req.GetRef()) == "HEAD" {
		if err := repo.SetDefaultBranch(ctx, s.txManager, git.ReferenceName(req.GetRevision())); err != nil {
			return fmt.Errorf("setting default branch: %w", err)
		}

		return nil
	}

	return updateRef(ctx, repo, req)
}
```

**File:** internal/gitaly/service/repository/write_ref.go (L56-78)
```go
	var newObjectID git.ObjectID
	if objectHash.IsZeroOID(git.ObjectID(req.GetRevision())) {
		// Passing the all-zeroes object ID as new value means that we should delete the
		// reference.
		newObjectID = objectHash.ZeroOID
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
	}
```

**File:** internal/gitaly/service/repository/write_ref.go (L132-152)
```go
func validateWriteRefRequest(ctx context.Context, locator storage.Locator, req *gitalypb.WriteRefRequest) error {
	if err := locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return err
	}
	if err := git.ValidateRevision(req.GetRef()); err != nil {
		return fmt.Errorf("invalid ref: %w", err)
	}
	if err := git.ValidateRevision(req.GetRevision()); err != nil {
		return fmt.Errorf("invalid revision: %w", err)
	}
	if len(req.GetOldRevision()) > 0 {
		if err := git.ValidateRevision(req.GetOldRevision()); err != nil {
			return fmt.Errorf("invalid OldRevision: %w", err)
		}
	}

	if !bytes.Equal(req.GetRef(), []byte("HEAD")) && !bytes.HasPrefix(req.GetRef(), []byte("refs/")) {
		return fmt.Errorf("ref has to be a full reference")
	}
	return nil
}
```

**File:** internal/git/localrepo/refs.go (L149-157)
```go
// SetDefaultBranch sets the repository's HEAD to point to the given reference.
// It will not verify the reference actually exists.
func (repo *Repo) SetDefaultBranch(ctx context.Context, txManager transaction.Manager, reference git.ReferenceName) error {
	if err := git.ValidateReference(reference.String()); err != nil {
		return fmt.Errorf("%q is a malformed refname", reference)
	}

	return repo.setDefaultBranchWithUpdateRef(ctx, reference)
}
```

**File:** internal/git/localrepo/refs.go (L159-188)
```go
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

**File:** internal/backup/repository.go (L459-473)
```go
// SetHeadReference sets the symbolic HEAD reference of the repository.
func (rr *remoteRepository) SetHeadReference(ctx context.Context, target git.ReferenceName) error {
	repoClient := rr.newRepoClient()

	_, err := repoClient.WriteRef(ctx, &gitalypb.WriteRefRequest{
		Repository: rr.repo,
		Ref:        []byte("HEAD"),
		Revision:   []byte(target),
	})
	if err != nil {
		return fmt.Errorf("write HEAD ref: %w", err)
	}

	return nil
}
```

**File:** internal/gitaly/service/repository/write_ref_test.go (L176-198)
```go
			desc: "revision refers to missing reference",
			setup: func(t *testing.T) setupData {
				repo, _ := gittest.CreateRepository(t, ctx, cfg)
				revision := []byte("refs/heads/missing")

				return setupData{
					request: &gitalypb.WriteRefRequest{
						Repository: repo,
						Ref:        []byte("refs/heads/main"),
						Revision:   revision,
					},
					expectedErr: structerr.NewNotFound("resolving new revision: reference not found").WithDetail(
						&gitalypb.ReferenceNotFoundError{
							ReferenceName: revision,
						},
					),
					expectedRefs: []git.Reference{
						git.NewSymbolicReference("HEAD", git.DefaultRef),
					},
					expectedVotes: []transaction.PhasedVote{},
				}
			},
		},
```

**File:** internal/gitaly/service/repository/write_ref_test.go (L250-279)
```go
		{
			desc: "update default branch",
			setup: func(t *testing.T) setupData {
				repo, repoPath := gittest.CreateRepository(t, ctx, cfg)

				defaultCommit := gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch(git.DefaultBranch))
				newCommit := gittest.WriteCommit(t, cfg, repoPath, gittest.WithBranch("new-default"))

				return setupData{
					request: &gitalypb.WriteRefRequest{
						Repository: repo,
						Ref:        []byte("HEAD"),
						Revision:   []byte("refs/heads/new-default"),
					},
					expectedRefs: []git.Reference{
						git.NewSymbolicReference("HEAD", "refs/heads/new-default"),
						git.NewReference(git.DefaultRef, defaultCommit),
						git.NewReference("refs/heads/new-default", newCommit),
					},
					expectedVotes: []transaction.PhasedVote{
						{Phase: voting.Prepared, Vote: voting.VoteFromData([]byte(
							fmt.Sprintf("%s ref:refs/heads/new-default HEAD\n", gittest.DefaultObjectHash.ZeroOID),
						))},
						{Phase: voting.Committed, Vote: voting.VoteFromData([]byte(
							fmt.Sprintf("%s ref:refs/heads/new-default HEAD\n", gittest.DefaultObjectHash.ZeroOID),
						))},
					},
				}
			},
		},
```
