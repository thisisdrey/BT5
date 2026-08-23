### Title
Unbounded mbox message count in UserApplyPatch causes memory/inode/CPU exhaustion via git-mailsplit fan-out - ([File: internal/gitaly/service/operations/apply_patch_mbox.go])

### Summary
`splitMailbox` pipes an attacker-controlled `Patches` stream directly into `git-mailsplit(1)` with no cap on the number of resulting messages, then loads every produced filename into memory via `os.ReadDir` and returns them as a `[]string`. `applyPatchesWithIndex` subsequently iterates the full list, creating and applying a patch per entry, giving an unprivileged user with push-level `UserApplyPatch` access a way to force unbounded file, memory, and CPU usage on the Gitaly node.

### Finding Description
`UserApplyPatch` accepts a header plus a streamed `Patches` byte stream from any caller authorized to push/apply patches to a repository they control [1](#0-0) . The stream is wrapped into an `io.Reader` and handed to `splitAndParseMailbox`/`splitMailbox` with no size or count limit imposed beforehand [2](#0-1) . `splitMailbox` runs `git-mailsplit(1)` against the entire attacker-supplied mbox body, which will happily split any number of minimal `From `-delimited messages into individual files named `0001`, `0002`, ... in `outDir` [3](#0-2) . After mailsplit finishes, `os.ReadDir(outDir)` is called and every directory entry is appended into a `messageFiles` slice with no upper bound check on `len(entries)` [4](#0-3) . `applyPatchesWithIndex` then loops over `patches` (one per split message), calling `os.Mkdir` for a per-patch work directory and invoking `git apply`/`git merge-tree` for each one, with no cap on `i` [5](#0-4) . There is no check anywhere in this code path — nor in `validateUserApplyPatchHeader` — that limits the number of mbox messages, the total mbox size, or the resulting fan-out of directories and subprocess invocations [6](#0-5) . The generic concurrency limiter middleware in the repo only throttles concurrent RPC invocations per type/repo, not the size or shape of a single request's payload, so it does not mitigate this.

### Impact Explanation
An attacker who can invoke `UserApplyPatch` (standard push/merge workflow privilege, no admin role) can submit a crafted mbox containing e.g. 100k tiny `From `-separated stub messages. This forces `git-mailsplit` to write 100k small files to a temp directory, `os.ReadDir` to materialize 100k filenames in memory, and the apply loop to create/remove 100k per-patch subdirectories and spawn multiple git subprocesses (`read-tree`, `apply`, `write-tree`) per patch — multiplying CPU, memory, file descriptor, and inode consumption on the storage volume. This can degrade or crash the Gitaly worker process handling the RPC and, if temp storage is shared, affect other repositories on the same storage shard, matching a resource-exhaustion/DoS-of-RPC-handler impact class.

### Likelihood Explanation
The only precondition is ordinary push/merge-level access to trigger `UserApplyPatch` against a repository the attacker controls (e.g., via a merge request "apply patch" flow), which is a standard unprivileged capability. The exploit requires no special git object crafting — a trivially small script can generate a large mbox of minimal messages — making it fully repeatable and low-effort to trigger repeatedly.

### Recommendation
Enforce an explicit upper bound on the number of mbox messages (and/or total mbox byte size) before or during `splitMailbox`/`applyPatchesWithIndex` — e.g., reject the request early if `len(entries)` from `os.ReadDir` exceeds a configured maximum, or count `From ` boundaries while streaming and abort once a threshold is crossed, returning `structerr.NewInvalidArgument` (or `ResourceExhausted`) so the fan-out never reaches the per-patch application loop.

### Proof of Concept
```go
func TestUserApplyPatch_hugeMailboxDoS(t *testing.T) {
    // Build a synthetic mbox: N minimal "From " separated stub messages,
    // each with just enough headers to pass mailsplit but not necessarily mailinfo.
    var mbox bytes.Buffer
    const n = 100000
    for i := 0; i < n; i++ {
        fmt.Fprintf(&mbox, "From nobody Mon Sep 17 00:00:00 2001\n"+
            "From: A <a@example.com>\nSubject: s%d\nDate: Mon, 1 Jan 2024 00:00:00 +0000\n\nbody\n", i)
    }

    stream, err := client.UserApplyPatch(ctx)
    require.NoError(t, err)
    require.NoError(t, stream.Send(&gitalypb.UserApplyPatchRequest{
        UserApplyPatchRequestPayload: &gitalypb.UserApplyPatchRequest_Header_{
            Header: &gitalypb.UserApplyPatchRequest_Header{
                Repository:   repo, User: user, TargetBranch: []byte("dos-branch"),
            },
        },
    }))
    // Stream the huge mbox in chunks and measure time/memory/fd usage until
    // the server OOMs, times out, or exhausts inodes in its temp dir.
    _, err = sendMboxInChunks(stream, mbox.Bytes())
    // Expect: server should reject with a resource-limit error quickly,
    // NOT attempt to materialize 100k files/dirs and apply 100k patches.
}
```
Expected (current, vulnerable) behavior: `git-mailsplit` writes 100k files into `workDir`, `os.ReadDir` returns a 100k-element slice, and `applyPatchesWithIndex` performs 100k `os.Mkdir`/git-subprocess cycles, consuming disproportionate CPU/memory/inodes with no early rejection.

### Citations

**File:** internal/gitaly/service/operations/apply_patch.go (L29-42)
```go
func (s *Server) UserApplyPatch(stream gitalypb.OperationService_UserApplyPatchServer) error {
	firstRequest, err := stream.Recv()
	if err != nil {
		return err
	}

	header := firstRequest.GetHeader()
	if header == nil {
		return structerr.NewInvalidArgument("empty UserApplyPatch_Header")
	}

	if err := validateUserApplyPatchHeader(stream.Context(), s.locator, header); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}
```

**File:** internal/gitaly/service/operations/apply_patch.go (L164-171)
```go
	mboxReader := streamio.NewReader(func() ([]byte, error) {
		req, err := stream.Recv()
		return req.GetPatches(), err
	})

	patches, err := splitAndParseMailbox(ctx, repo, mboxReader, workDir)
	if err != nil {
		return "", fmt.Errorf("parsing mailbox: %w", err)
```

**File:** internal/gitaly/service/operations/apply_patch.go (L174-219)
```go
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
```

**File:** internal/gitaly/service/operations/apply_patch.go (L398-411)
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
```

**File:** internal/gitaly/service/operations/apply_patch_mbox.go (L49-65)
```go
func splitMailbox(ctx context.Context, repo *localrepo.Repo, mboxReader io.Reader, outDir string) ([]string, error) {
	var stdout, stderr bytes.Buffer
	if err := repo.ExecAndWait(ctx,
		gitcmd.Command{
			Name: "mailsplit",
			Flags: []gitcmd.Option{
				// This is a bit awkward, but we cannot use a ValueFlag
				// here because git-mailsplit(1) doesn't use parse-opts.
				gitcmd.Flag{Name: "-o" + outDir},
			},
		},
		gitcmd.WithStdin(mboxReader),
		gitcmd.WithStdout(&stdout),
		gitcmd.WithStderr(&stderr),
	); err != nil {
		return nil, fmt.Errorf("mailsplit: %w, stderr: %q", err, stderr.String())
	}
```

**File:** internal/gitaly/service/operations/apply_patch_mbox.go (L67-85)
```go
	entries, err := os.ReadDir(outDir)
	if err != nil {
		return nil, fmt.Errorf("reading mailsplit output: %w", err)
	}

	// git-mailsplit(1) writes the messages into zero-padded, monotonically
	// increasing files like "0001", "0002". Sorting the names lexically thus
	// restores the original order.
	messageFiles := make([]string, 0, len(entries))
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		messageFiles = append(messageFiles, filepath.Join(outDir, entry.Name()))
	}
	sort.Strings(messageFiles)

	return messageFiles, nil
}
```
