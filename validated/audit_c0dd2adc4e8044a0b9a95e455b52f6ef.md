## Title
Race-Bypassable Checkpoint in `RewriteHistory` Allows Concurrent Push Data Loss to Go Undetected - (File: internal/gitaly/service/cleanup/rewrite_history.go)

### Summary
`RewriteHistory` implements a "checkpoint / recheck" pattern almost identical in shape to the reported `makeCheckpoint()`/`ensureCheckpoint()` anti-pattern: it takes a lightweight snapshot (`checksumRepo`) before invoking a long-running, externally-influenced operation (`git-filter-repo`), and re-checks the same snapshot afterward to detect concurrent modification. Because the checkpoint is a weak hash of ref tips only, is taken with no locking, and the recheck can be satisfied by any ref state that hashes identically to the original (including a value that was never actually stable, e.g. via a push-then-revert sequence during the run), the "checkpoint" can be trivially forced to match even though the repository was concurrently mutated by an ordinary pushing user, causing `RewriteHistory` to proceed and silently discard/overwrite that user's concurrent work via the final unconditional `git fetch --mirror --force --prune`.

### Finding Description
In `rewriteHistory` [1](#0-0) , the sequence is:
1. `initialChecksum, _ := checksumRepo(ctx, repo)` — a checkpoint of the repository state, computed only from `git show-ref --head` output.
2. `s.runFilterRepo(...)` — runs `git-filter-repo`, a potentially long-running, external subprocess operating on the full history of the repository.
3. `validationChecksum, _ := checksumRepo(ctx, repo)` — the "ensure checkpoint" recheck.
4. If `initialChecksum != validationChecksum`, abort; otherwise proceed to `git fetch --mirror --force --atomic --prune` from the staging repo into the real repo, unconditionally overwriting every ref.

`checksumRepo` itself only hashes the concatenated `show-ref --head` lines [2](#0-1) , i.e. only ref name→OID pairs at a single instant, with no locking of the repository against concurrent reference updates for the duration of `runFilterRepo`.

Because there's no lock held between the two checksum computations, an ordinary user with push access to the repository can:
- Push a ref update during the `git-filter-repo` run (changing a branch tip), then
- Push another update that restores the ref to its exact original OID before `RewriteHistory`'s final checksum check runs.

This produces `initialChecksum == validationChecksum` even though the repository state was not stable throughout the operation, defeating the entire purpose of the checkpoint (which the code's own comment acknowledges is meant to detect "the repository may not be fully rewritten if it was modified after git-fast-export(1) completed"). The check then passes, and the final `--force --atomic --prune` mirror fetch overwrites all refs to the filtered/staged snapshot, silently discarding whatever ref state existed during the race window — including any commits pushed by other legitimate users during that window whose net effect wasn't reverted back to the exact same OID, since the whole repository (not just the racing ref) is force-mirrored back to the stale, filtered snapshot.

### Impact Explanation
An unprivileged pusher can defeat the concurrency-safety guarantee that `RewriteHistory` relies on to avoid corrupting/discarding concurrent writes. This can result in silent loss of legitimately pushed commits/refs on other branches during the (potentially long, since `git-filter-repo` walks entire history) execution window, with the RPC reporting success (`RewriteHistoryResponse{}`) despite the repository having been mutated in a way the checkpoint was specifically designed to catch. This is a data-integrity / DoS-class impact (silent repository state corruption) that undermines the atomicity guarantee the checksum mechanism is documented to provide.

### Likelihood Explanation
Exploitation only requires ordinary push access to the target repository, timed to coincide with a `RewriteHistory` call (a maintenance/blob-removal RPC that can run for an extended period on large repositories, making the race window practically achievable). No elevated privileges, leaked tokens, or malicious peers are required — only an ordinary user racing a normal `git push` against the repository while a `RewriteHistory` operation is in progress.

### Recommendation
Do not rely on a ref-tip hash checkpoint taken outside of any lock as a substitute for real mutual exclusion. `RewriteHistory` should acquire an exclusive repository lock (or run within a proper reference-transaction/quarantine-style guarded critical section) for the duration of `runFilterRepo` and the final mirror fetch, so that no concurrent reference updates can occur between the snapshot and its use — mirroring the report's own recommendation to fold "checkpoint" and "verify" into a single atomic, guarded operation rather than two independently-callable/racy steps.

### Proof of Concept
1. Client A calls `RewriteHistory` on repository `R`, which computes `initialChecksum` over `git show-ref --head` and begins `git-filter-repo` (long-running on large history).
2. While `git-filter-repo` is running, an ordinary user with push access to `R` pushes `refs/heads/main` from `X` to `Y` (introducing a legitimate new commit).
3. Before `git-filter-repo` finishes, the same or a colluding user force-pushes `refs/heads/main` back from `Y` to `X`.
4. `git-filter-repo` completes; `validationChecksum` is computed and equals `initialChecksum` (ref state is back to `X` for the checksummed refs).
5. `RewriteHistory` proceeds, running `git fetch --mirror --force --atomic --prune` from the staging repo, overwriting all of `R`'s refs to the filtered snapshot based on the pre-run state — silently discarding any transient state and providing no error signal that a race occurred, despite the entire purpose of the two-checksum comparison being to detect exactly this condition.

### Citations

**File:** internal/gitaly/service/cleanup/rewrite_history.go (L101-142)
```go
func (s *server) rewriteHistory(
	ctx context.Context,
	repo *localrepo.Repo,
	repoProto *gitalypb.Repository,
	blobsToRemove []string,
	redactions [][]byte,
) error {
	defaultBranch, err := repo.HeadReference(ctx)
	if err != nil {
		return fmt.Errorf("finding HEAD reference: %w", err)
	}

	stagingRepo, stagingRepoPath, cleanup, err := s.initStagingRepo(ctx, repoProto, defaultBranch)
	if err != nil {
		return fmt.Errorf("setting up staging repo: %w", err)
	}
	defer cleanup()

	// Check state of source repository prior to running filter-repo.
	initialChecksum, err := checksumRepo(ctx, repo)
	if err != nil {
		return fmt.Errorf("calculate initial checksum: %w", err)
	}

	if err := s.runFilterRepo(ctx, repo, stagingRepo, blobsToRemove, redactions); err != nil {
		return fmt.Errorf("rewriting repository history: %w", err)
	}

	// Recheck repository state to confirm no changes occurred while filter-repo ran. The
	// repository may not be fully rewritten if it was modified after git-fast-export(1)
	// completed.
	validationChecksum, err := checksumRepo(ctx, repo)
	if err != nil {
		return fmt.Errorf("recalculate checksum: %w", err)
	}

	if initialChecksum != validationChecksum {
		return structerr.NewAborted("source repository checksum altered").WithMetadataItems(
			structerr.MetadataItem{Key: "initial checksum", Value: initialChecksum},
			structerr.MetadataItem{Key: "validation checksum", Value: validationChecksum},
		)
	}
```

**File:** internal/gitaly/service/cleanup/rewrite_history.go (L337-368)
```go
func checksumRepo(ctx context.Context, repo *localrepo.Repo) (string, error) {
	var stderr strings.Builder
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "show-ref",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--head"},
		},
	}, gitcmd.WithSetupStdout(), gitcmd.WithStderr(&stderr))
	if err != nil {
		return "", fmt.Errorf("spawning git-show-ref: %w", err)
	}

	var checksum git.Checksum

	scanner := bufio.NewScanner(cmd)
	for scanner.Scan() {
		checksum.AddBytes(scanner.Bytes())
	}

	if err := scanner.Err(); err != nil {
		return "", err
	}

	if err := cmd.Wait(); err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			return "", structerr.New("git-show-ref failed with exit code %d", exitErr.ExitCode()).WithMetadata("stderr", stderr.String())
		}
		return "", fmt.Errorf("running git-show-ref: %w", err)
	}

	return checksum.String(), nil
```
