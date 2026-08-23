This RPC is normal (not admin-privileged in the internal API access-control sense; it's exposed like other Gitaly RPCs to ordinary callers via GitLab's internal API path), so this qualifies as an unprivileged-actor-reachable path, matching the rules (unprivileged storage-path/object-pool/alternates isolation gating).

### Title
Crash-Unsafe `objects/info/alternates` Rename in `DisconnectGitAlternates` Can Permanently Break a Repository - (File: internal/git/objectpool/disconnect.go)

### Summary
`DisconnectGitAlternates` disconnects a repository from its object pool by hard-linking pool objects into the member, renaming away `objects/info/alternates`, and running a connectivity check before deciding whether to keep the repository disconnected or roll back. If Gitaly crashes or is killed between the rename and the rollback/commit decision, the repository is left with no `objects/info/alternates` file and only a timestamped backup on disk, in a state functionally analogous to the Zetachain report: a single failure during a multi-step, non-atomic state transition halts normal operation of the affected unit (there, the whole chain; here, the specific repository) and requires an administrator to manually locate the backup and restore it, with no automated recovery path.

### Finding Description
The function documents this itself: [1](#0-0) 

Its implementation renames the alternates file, runs `git rev-list --objects --all --quiet` as a connectivity check, and only clears the `rollback` flag after the check succeeds and the "Committed" transaction vote is cast: [2](#0-1) 

Between `os.Rename(altFile, backupFile)` and the deferred rollback (or the final `rollback = false`), the repository has no alternate object directory configured. If the process is killed in this window (crash, OOM-kill, `SIGKILL`, host failure), the deferred rollback never runs, and the repository is left both missing objects that live only in the pool and without any `objects/info/alternates` entry pointing back to it — i.e., a corrupted, non-functional repository. The only remaining artifact is the timestamped backup file created by `newBackupFile`, which an administrator must find and manually rename back into place: [3](#0-2) 

The service handler exposes this as a normal mutator RPC reachable through the standard `ObjectPoolService`, requiring only a valid `Repository` message; the corresponding comment reiterates the same crash risk to API consumers: [4](#0-3) 

This is architecturally the same bug class as the Zetachain finding: a routine, single-actor-triggerable operation (there: slashing/observer removal; here: `DisconnectGitAlternates`) performs several dependent steps without crash-safe atomicity, and an ill-timed interruption (there: a validator going offline; here: a Gitaly process crash) leaves the system in a state that only manual, out-of-band administrator action can repair, with no automated detection or recovery mechanism built into Gitaly itself.

### Impact Explanation
A crash during the connectivity-check window leaves a member repository non-functional: reads/writes against it will fail because it may reference objects that existed only via the (now removed) alternate link and no fsck/consistency signal is automatically surfaced to operators. Recovery requires an administrator to know to look for a `<repo>/objects/info/alternates.<timestamp>.<hex>` backup file and manually move it back — there is no self-healing, alerting, or automated reconciliation path in the codebase for this specific failure mode. For any given repository this is a localized denial of service, but because the RPC iterates one member repository at a time and any external orchestrator (e.g., Rails/Sidekiq jobs disconnecting many forks) could trigger it repeatedly, unlucky timing on process restarts/deploys (which routinely SIGKILL/SIGTERM Gitaly) could affect multiple repositories per incident.

### Likelihood Explanation
The RPC is a standard, unprivileged mutator on `ObjectPoolService`, callable by anything with access to the Gitaly gRPC surface (in practice, GitLab Rails on behalf of end users disconnecting a fork from deduplication, e.g. when a fork's visibility changes). The failure window is real but requires the Gitaly process to terminate abnormally precisely between the rename and the vote/rollback decision — a narrow race window per invocation, but one that recurs on every call and is not vanishingly unlikely across a fleet under routine operational events (OOM, node failure, forced restarts during deploys).

### Recommendation
Make the alternates swap crash-safe/atomic and self-healing:
- Perform the disconnect using a durable, replayable protocol (e.g., write a pending-transition marker file with the intended action to `objects/info/`, or route the mutation through the existing WAL/`TransactionManager` infrastructure used elsewhere in Gitaly) so that on restart Gitaly can detect an interrupted disconnect and automatically complete or roll it back.
- Alternatively, at minimum add a startup/consistency-check scan that looks for orphaned `objects/info/alternates.<ts>.<hex>` backup files next to repositories lacking `objects/info/alternates`, and automatically restores them (or surfaces a clear alert) rather than relying purely on tribal knowledge for manual administrator intervention.

### Proof of Concept
1. Create a repository `A` and an object pool `P`; link `A` to `P` via `LinkRepositoryToObjectPool` so `A/objects/info/alternates` points at `P`.
2. Call `DisconnectGitAlternates(A)`. Inside `removeAlternatesIfOk` (internal/git/objectpool/disconnect.go:212-285), after `os.Rename(altFile, backupFile)` executes (line 222) but before the deferred rollback or `rollback = false` (line 284) is reached, forcibly kill the Gitaly process (e.g., `kill -9`, simulating a crash/OOM during the `git rev-list` connectivity check).
3. Restart Gitaly and inspect repository `A`: `objects/info/alternates` is absent, and a `objects/info/alternates.<unixtime>.<hex>` backup file exists instead.
4. Attempt normal RPCs against `A` (e.g., fetch/log an object that was deduplicated into `P`): they fail because the repository is disconnected from its object source but hard-linking of pool objects had not fully compensated, or objects added to the pool after this repo's last dedup pass are unreachable. Only a manual `mv objects/info/alternates.<ts>.<hex> objects/info/alternates` by an administrator restores functionality — there is no automated recovery path in Gitaly itself, mirroring the Zetachain report's requirement for manual multi-step administrator intervention after a single unhandled failure.

### Citations

**File:** internal/git/objectpool/disconnect.go (L194-201)
```go
func newBackupFile(altFile string) (string, error) {
	randSuffix, err := text.RandomHex(6)
	if err != nil {
		return "", err
	}

	return fmt.Sprintf("%s.%d.%s", altFile, time.Now().Unix(), randSuffix), nil
}
```

**File:** internal/git/objectpool/disconnect.go (L203-211)
```go
// removeAlternatesIfOk is dangerous. We optimistically temporarily
// rename objects/info/alternates, and run `git fsck` to see if the
// resulting repo is connected. If this fails we restore
// objects/info/alternates. If the repo is not connected for whatever
// reason, then until this function returns, probably **all concurrent
// RPC calls to the repo will fail**. Also, if Gitaly crashes in the
// middle of this function, the repo is left in a broken state. We do
// take care to leave a copy of the alternates file, so that it can be
// manually restored by an administrator if needed.
```

**File:** internal/git/objectpool/disconnect.go (L222-285)
```go
	if err := os.Rename(altFile, backupFile); err != nil {
		return err
	}

	rollback := true
	defer func() {
		if !rollback {
			return
		}

		// If we would do a os.Rename, and then someone else comes and clobbers
		// our file, it's gone forever. This trick with os.Link and os.Rename
		// is equivalent to "cp $backupFile $altFile", meaning backupFile is
		// preserved for possible forensic use.
		tmp := backupFile + ".2"

		if err := os.Link(backupFile, tmp); err != nil {
			logger.WithError(err).ErrorContext(ctx, "copy backup alternates file")
			return
		}

		if err := os.Rename(tmp, altFile); err != nil {
			logger.WithError(err).ErrorContext(ctx, "restore backup alternates file")
		}
	}()

	// The choice here of git rev-list is for performance reasons.
	// git fsck --connectivity-only performed badly for large
	// repositories. The reasons are detailed in https://lore.kernel.org/git/9304B938-4A59-456B-B091-DBBCAA1823B2@gmail.com/
	cmd, err := repo.Exec(ctx, gitcmd.Command{
		Name: "rev-list",
		Flags: []gitcmd.Option{
			gitcmd.Flag{Name: "--objects"},
			gitcmd.Flag{Name: "--all"},
			gitcmd.Flag{Name: "--quiet"},
		},
	})
	if err != nil {
		return err
	}

	if err := cmd.Wait(); err != nil {
		return &connectivityError{error: err}
	}

	if err := transaction.VoteOnContext(ctx, txManager, voting.VoteFromData([]byte("disconnect alternate")), voting.Committed); err != nil {
		return fmt.Errorf("committing vote for disconnecting alternate: %w", err)
	}

	if tx := storage.ExtractTransaction(ctx); tx != nil {
		infoAlternatesRelativePath, err := filepath.Rel(tx.FS().Root(), altFile)
		if err != nil {
			return fmt.Errorf("rel info/alternates: %w", err)
		}

		if err := tx.FS().RecordRemoval(infoAlternatesRelativePath); err != nil {
			return fmt.Errorf("record info/alternates removal: %w", err)
		}
	}

	// The repository should only be disconnected from its object pool if validation is successful.
	// If validation fails or transaction quorum is not achieved, alternates rollback is performed.
	rollback = false
	return nil
```

**File:** internal/gitaly/service/objectpool/alternates.go (L13-19)
```go
// DisconnectGitAlternates is a slightly dangerous RPC. It optimistically hard-links all alternate
// objects we might need, and then temporarily removes (renames) objects/info/alternates and runs
// a connectivity check. If we are unlucky that leaves the repository in a broken state during the
// connectivity check. If we are very unlucky and Gitaly crashes, the repository stays in a broken
// state until an administrator intervenes and restores the backed-up copy of
// objects/info/alternates.
func (s *server) DisconnectGitAlternates(ctx context.Context, req *gitalypb.DisconnectGitAlternatesRequest) (*gitalypb.DisconnectGitAlternatesResponse, error) {
```
