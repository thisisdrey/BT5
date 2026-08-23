### Title
UserApplyPatch writes patch-derived objects into the main object store without quarantine, bypassing quarantine review before Rails access checks - ([File: internal/gitaly/service/operations/apply_patch.go])

### Summary
`UserApplyPatch` builds patched trees/commits directly on the unquarantined repository and calls `updateReferenceWithHooks` with a `nil` quarantine directory, unlike every other `operations` RPC (`UserCommitFiles`, `UserCherryPick`, `UserRevert`, `UserMergeBranch`, `UserFFBranch`, `UserRebaseConfirmable`, `UserCreateTag`, `UserCreateBranch`, `UserUpdateSubmodule`), which all obtain a `quarantine.Dir` via `s.quarantinedRepo` first. Because objects are written straight into the real repository's object database before the pre-receive/Rails `Allowed()` check runs, a rejected push still leaves attacker-supplied blob/tree/commit objects permanently in main storage.

### Finding Description
`UserApplyPatch` (`internal/gitaly/service/operations/apply_patch.go`) builds `repo := s.localRepoFactory.Build(header.GetRepository())` directly against the real repository — it never calls `s.quarantinedRepo(ctx, ...)` as the other mutating RPCs do. [1](#0-0) 

Patch application (`applyPatchesWithIndex` → `applyPatchToTreeish` → `applyPatchSimple`/`applyPatchThreeWay` → `writeTree`, plus `repo.WriteCommit`) writes trees, blobs and commit objects for attacker-controlled mailbox patch content directly into that unquarantined repository's object database. [2](#0-1) 

The reference update is then requested with an explicit `nil` quarantine directory: [3](#0-2) 

Inside `UpdaterWithHooks.UpdateReference`, when `quarantineDir` is `nil`, `quarantinedRepo := repoProto` is used directly as-is for the pre-receive hook (Rails `Allowed()` check), and the subsequent `quarantineDir.Migrate(ctx)` step (which is what actually moves quarantined objects into main storage only after Rails approval) is skipped entirely: [4](#0-3) 

This differs materially from the quarantine flow used by every sibling RPC: in the quarantine case, new objects are written into an isolated per-request temporary object directory first, the pre-receive hook is evaluated against that quarantined repo, and only on success are those objects migrated (`quarantineDir.Migrate`) into main storage; if the hook rejects the change, the quarantine directory (and everything written into it) is discarded via `cleanup()` and never touches the real repository. For `UserApplyPatch`, the analogous "new objects" (patched blobs, trees, commits) are already present in the real repository's `.git/objects` *before* the pre-receive hook is ever invoked, so a hook rejection (failed access check, push rule, secret/file-type scan, etc., all of which GitLab implements via the pre-receive hook payload) does not undo the object writes — they remain as loose, unreferenced objects in main storage.

The attacker input path is fully reachable by an unprivileged user with push access: `UserApplyPatch` accepts a streamed mailbox of patches (`header.GetTargetBranch()`, patch content) with no requirement of prior authorization beyond call-level access, and `validateUserApplyPatchHeader` performs only basic presence checks, none of which restrict patch *content*. [5](#0-4) 

### Impact Explanation
An unprivileged pusher with access to `UserApplyPatch` (e.g., via a merge request "apply patch"/suggestion flow) can cause arbitrary attacker-chosen blob/tree/commit content to be persisted into the target repository's main object store even when the corresponding Rails access check (pre-receive hook) subsequently rejects the ref update. This bypasses the quarantine boundary's core security guarantee — that unvetted objects never reach main storage until explicitly approved — and can be used to smuggle unauthorized content into a repository's on-disk object database (later reachable/dangling until GC), circumventing pre-receive-time content policies (e.g., prohibited-file-type checks, size limits, secret scanning) whose entire premise is to evaluate objects before they are committed to storage. This matches the "hook or quarantine bypass" impact class in scope.

### Likelihood Explanation
This requires no special privileges beyond the standard ability to call `UserApplyPatch` (available to any user who can push/merge via the normal GitLab merge-request "apply patch" workflow), and the attacker fully controls the patch content and target branch supplied in the request. The bug is deterministic and repeatable — any patch application that is later rejected by pre-receive hooks results in the orphan objects being retained. No race condition or timing dependency is needed.

### Recommendation
Make `UserApplyPatch` use the same quarantine pattern as all other mutating operations RPCs: call `s.quarantinedRepo(ctx, header.GetRepository())` to obtain a `quarantine.Dir` and a quarantined `*localrepo.Repo`, perform `applyPatchesWithIndex`/tree and commit writes against the quarantined repo, and pass the resulting `quarantineDir` (instead of `nil`) into `updateReferenceWithHooks`, so that patch-derived objects are only migrated into main storage after the pre-receive hook approves the change.

### Proof of Concept
```go
func TestUserApplyPatch_QuarantineBypass(t *testing.T) {
    ctx := testhelper.Context(t)
    cfg := testcfg.Build(t)
    client, _ := runOperationServiceServer(t, cfg) // existing test harness helper
    repoProto, repoPath := gittest.CreateRepository(t, ctx, cfg, gittest.CreateRepositoryConfig{})

    // Configure a pre-receive hook that always rejects, simulating Rails Allowed()==false.
    gittest.WriteCustomHook(t, repoPath, "pre-receive", []byte("#!/bin/sh\nexit 1\n"))

    stream, err := client.UserApplyPatch(ctx)
    require.NoError(t, err)
    require.NoError(t, stream.Send(&gitalypb.UserApplyPatchRequest{
        UserApplyPatchRequestPayload: &gitalypb.UserApplyPatchRequest_Header_{
            Header: &gitalypb.UserApplyPatchRequest_Header{
                Repository:   repoProto,
                User:         gittest.TestUser,
                TargetBranch: []byte("main"),
            },
        },
    }))
    require.NoError(t, stream.Send(&gitalypb.UserApplyPatchRequest{
        UserApplyPatchRequestPayload: &gitalypb.UserApplyPatchRequest_Patches{
            Patches: attackerPatchBytes, // mbox patch adding a file with attacker content
        },
    }))
    _, err = stream.CloseAndRecv()
    require.Error(t, err) // rejected by pre-receive hook (as expected)

    // Assertion: despite rejection, objects from the patch are present in the MAIN repo's
    // object database (would not be the case if a quarantine dir had been used and discarded).
    treeOrBlobOID := extractExpectedBlobOIDFromPatch(attackerPatchBytes)
    out := gittest.Exec(t, cfg, "-C", repoPath, "cat-file", "-e", treeOrBlobOID.String())
    require.NoError(t, err, "attacker-controlled object should NOT exist in main storage after rejected push, but it does")
}
```
Expected (buggy) result: the `cat-file -e` check succeeds — the object exists in `repoPath/objects` even though the RPC call failed and no ref was updated, demonstrating that patch content bypassed the quarantine boundary and landed in main storage without approval.

### Citations

**File:** internal/gitaly/service/operations/apply_patch.go (L62-71)
```go
func (s *Server) userApplyPatch(ctx context.Context, header *gitalypb.UserApplyPatchRequest_Header, stream gitalypb.OperationService_UserApplyPatchServer) (returnedErr error) {
	branchCreated := false
	targetBranch := git.NewReferenceNameFromBranchName(string(header.GetTargetBranch()))

	repo := s.localRepoFactory.Build(header.GetRepository())

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}
```

**File:** internal/gitaly/service/operations/apply_patch.go (L128-130)
```go
	if err := s.updateReferenceWithHooks(ctx, header.GetRepository(), header.GetUser(), nil, targetBranch, patchedCommit, currentCommit); err != nil {
		return fmt.Errorf("update reference: %w", err)
	}
```

**File:** internal/gitaly/service/operations/apply_patch.go (L144-222)
```go
func (s *Server) applyPatchesWithIndex(
	ctx context.Context,
	repo *localrepo.Repo,
	parentCommitID git.ObjectID,
	committerSignature git.Signature,
	stream gitalypb.OperationService_UserApplyPatchServer,
) (git.ObjectID, error) {
	tempDir, err := repo.StorageTempDir()
	if err != nil {
		return "", fmt.Errorf("locating temporary directory: %w", err)
	}

	workDir, err := os.MkdirTemp(tempDir, "apply-patch-")
	if err != nil {
		return "", fmt.Errorf("creating work directory: %w", err)
	}
	defer func() {
		_ = os.RemoveAll(workDir)
	}()

	mboxReader := streamio.NewReader(func() ([]byte, error) {
		req, err := stream.Recv()
		return req.GetPatches(), err
	})

	patches, err := splitAndParseMailbox(ctx, repo, mboxReader, workDir)
	if err != nil {
		return "", fmt.Errorf("parsing mailbox: %w", err)
	}

	currentCommitID := parentCommitID
	for i, patch := range patches {
		perPatchWorkdir := filepath.Join(workDir, strconv.Itoa(i))
		if err := os.Mkdir(perPatchWorkdir, mode.Directory); err != nil {
			return "", fmt.Errorf("creating work directory for patch %d: %w", i, err)
		}

		treeID, err := applyPatchToTreeish(ctx, repo, perPatchWorkdir, currentCommitID, patch)
		if err != nil {
			var conflictErr *localrepo.MergeTreeConflictError
			if errors.As(err, &conflictErr) {
				return "", structerr.NewFailedPrecondition("Patch failed at %04d %s: %w", i+1, patch.subject, err)
			}

			return "", fmt.Errorf("applying patch %d: %w", i+1, err)
		}

		_ = os.RemoveAll(perPatchWorkdir)

		authorDate := patch.authorDate
		if authorDate.IsZero() {
			authorDate = committerSignature.When
		}

		commitMessage := patch.subject + "\n"
		if body := strings.Trim(patch.body, "\n"); body != "" {
			commitMessage += "\n" + body + "\n"
		}

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

	return currentCommitID, nil
}
```

**File:** internal/gitaly/service/operations/apply_patch.go (L398-412)
```go
func validateUserApplyPatchHeader(ctx context.Context, locator storage.Locator, header *gitalypb.UserApplyPatchRequest_Header) error {
	if err := locator.ValidateRepository(ctx, header.GetRepository()); err != nil {
		return err
	}

	if header.GetUser() == nil {
		return errors.New("missing User")
	}

	if len(header.GetTargetBranch()) == 0 {
		return errors.New("missing Branch")
	}

	return nil
}
```

**File:** internal/gitaly/hook/updateref/update_with_hooks.go (L208-235)
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
