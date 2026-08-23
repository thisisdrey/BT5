### Title
Non-fast-forward heuristic in `fetchRemoteAtomic` lets any `git-fetch` failure with empty stderr be silently treated as a partial success, migrating unverified quarantined objects - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`fetchRemoteAtomic`, the handler backing the `FetchRemote` RPC, distinguishes a "benign" `git-fetch(1)` failure (some ref updates rejected) from a "real" failure purely by checking whether `stderr` is empty and whether the error string is literally `"exit status 1"`. If both conditions hold, the code assumes the fetch's only problem was a failed reference update and proceeds to parse the (possibly incomplete) porcelain output, migrate the quarantine directory into the main repository, and commit reference updates — without ever verifying that the fetched object set is actually complete and valid.

### Finding Description
`quarantineRepo.FetchRemote(ctx, "inmemory", opts)` is invoked with `DryRun: true` and `Porcelain: true` so that objects are written into a `quarantine.Dir` before any references are updated [1](#0-0) . When the underlying `git-fetch` process returns an error, the handler applies a heuristic instead of a hard failure path: [2](#0-1) 

The comment explicitly states the intent — "successful reference updates should still be applied ... it is assumed the error is from a failed reference update" — but the only evidence used to reach that conclusion is the absence of stderr text and the literal string `"exit status 1"`. This is analogous to the ERC20 report's core defect: trusting a shallow success/failure signal (`success == true` on `call()`, or here "stderr is empty") instead of verifying the actual effect of the operation (the token balance actually increased; here, that the fetched object set is actually complete/consistent). Any failure mode of `git-fetch` that exits with status 1 without writing to stderr — for example a truncated/partial pack transfer, an interrupted network fetch that still returns exit code 1, or transport-layer conditions that don't populate stderr — will be misclassified as "just a failed ref update" and the handler will continue to:
1. Scan the (possibly incomplete) `--porcelain` stdout for ref updates [3](#0-2) .
2. Migrate the quarantine directory's objects into the main repository [4](#0-3) .
3. Commit the queued reference updates [5](#0-4) .

The quarantine mechanism is documented as providing atomicity precisely so that a failed fetch leaves the repository untouched [6](#0-5) , and `fetchRemoteAtomic`'s own comment states the same goal [7](#0-6) . The `err.Error() != "exit status 1"` / `errMsg != ""` heuristic is the single gate protecting that atomicity guarantee, and it is a proxy for success rather than a verification of it.

### Impact Explanation
`FetchRemote` is reachable from an ordinary, unprivileged flow: any user who can configure or trigger a repository's pull/push mirror (a standard, non-administrative operation) can cause Gitaly to run this code path against an attacker-influenced or simply unreliable remote URL. If the fetch fails in a way that produces exit status 1 with no stderr output but with a partial/incomplete object transfer, the quarantine's atomicity contract is broken: partially-fetched, unverified objects are migrated into the target repository and partial reference updates are committed. This can leave the target repository in an inconsistent state (dangling/incomplete object references, or refs pointing at not-fully-validated object graphs), directly undermining the same "verify the actual result, don't trust the surface-level status" guarantee the ERC20 report calls out for `_transferFromERC20`.

### Likelihood Explanation
The heuristic depends only on exit code and stderr contents of a subprocess, both of which are influenced by whatever remote/transport is configured for the fetch (`req.GetRemoteParams().GetUrl()`), and by network conditions during the RPC's execution — no special privilege is required to point `FetchRemote` at an arbitrary or unreliable endpoint. However, triggering the *exact* combination of "exit status 1 with truly empty stderr" is dependent on Git and transport behavior in edge/failure conditions, which is not fully within the caller's precise control, moderating the likelihood.

### Recommendation
Instead of pattern-matching on the *absence* of an error message, positively verify what happened: parse the porcelain output to confirm which ref updates failed (`RefUpdateTypeUpdateFailed`) versus succeeded, and only allow migration/commit to proceed once it's established that no unrelated object-transfer failure occurred. Consider also validating fetched objects (e.g., via `index-pack`/`fsck`-style checks) before migrating the quarantine directory into the repository, rather than relying on `git-fetch`'s exit status/stderr emptiness as an implicit success signal.

### Proof of Concept
Not directly reproducible without control over `git-fetch`'s failure behavior against an unreliable/attacker-influenced remote; the vulnerable logic is in the exit-status/stderr heuristic shown above [2](#0-1) , which any `FetchRemote` caller configuring a remote URL can exercise.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L51-73)
```go
func (s *server) fetchRemoteAtomic(ctx context.Context, req *gitalypb.FetchRemoteRequest) (_ bool, _ bool, returnedErr error) {
	var stdout, stderr bytes.Buffer
	opts := localrepo.FetchOpts{
		Stdout:  &stdout,
		Stderr:  &stderr,
		Force:   req.GetForce(),
		Prune:   !req.GetNoPrune(),
		Tags:    localrepo.FetchOptsTagsAll,
		Verbose: true,
		// Transactions are disabled during fetch operation because no references are updated when
		// the dry-run option is enabled. Instead, the reference-transaction hook is performed
		// during the subsequent execution of `git-update-ref(1)`.
		DisableTransactions: true,
		// When the `dry-run` option is used with `git-fetch(1)`, Git objects are received without
		// performing reference updates. This is used to quarantine objects on the initial fetch and
		// migration to occur only during reference update.
		DryRun: true,
		// The `porcelain` option outputs reference update information from `git-fetch(1) to stdout.
		// Since references are not updated during a `git-fetch(1)` dry-run, the reference
		// information is used during `git-update-ref(1)` execution to update the appropriate
		// corresponding references.
		Porcelain: true,
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L91-98)
```go
	// When performing fetch, objects are received before references are updated. If references fail
	// to be updated, unreachable objects could be left in the repository that would need to be
	// garbage collected. To be more atomic, a quarantine directory is set up where objects will be
	// fetched prior to being migrated to the main repository when reference updates are committed.
	quarantineDir, quarantineCleanup, err := quarantine.New(ctx, req.GetRepository(), s.logger, s.locator)
	if err != nil {
		return false, false, fmt.Errorf("creating quarantine directory: %w", err)
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L104-122)
```go
	if err := quarantineRepo.FetchRemote(ctx, "inmemory", opts); err != nil {
		// When `git-fetch(1)` fails to apply all reference updates successfully, the command
		// returns `exit status 1`. Despite this error, successful reference updates should still be
		// applied during the subsequent `git-update-ref(1)`. To differentiate between regular
		// errors and failed reference updates, stderr is checked for an error message. If an error
		// message is present, it is determined that an error occurred and the operation halts.
		errMsg := stderr.String()
		if errMsg != "" {
			return false, false, structerr.NewInternal("fetch remote: %q: %w", errMsg, err)
		}

		// Some errors during the `git-fetch(1)` operation do not print to stderr. If the error
		// message is not `exit status 1`, it is determined that the error is unrelated to failed
		// reference updates and the operation halts. Otherwise, it is assumed the error is from a
		// failed reference update and the operation proceeds to update references.
		if err.Error() != "exit status 1" {
			return false, false, structerr.NewInternal("fetch remote: %w", err)
		}
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L175-210)
```go
	// Parse stdout to identify required reference updates. Reference updates are queued to the
	// respective updater based on type.
	scanner := gitcmd.NewFetchPorcelainScanner(&stdout, objectHash)
	for scanner.Scan() {
		status := scanner.StatusLine()

		switch status.Type {
		// Failed and unchanged reference updates do not need to be applied.
		case gitcmd.RefUpdateTypeUpdateFailed, gitcmd.RefUpdateTypeUnchanged:
		// Queue pruned references in a separate transaction to avoid F/D conflicts.
		case gitcmd.RefUpdateTypePruned:
			if err := prunedUpdater.Delete(git.ReferenceName(status.Reference)); err != nil {
				return false, false, fmt.Errorf("queueing pruned ref for deletion: %w", err)
			}
			referencesUpdated = true
		// Queue all other reference updates in the same transaction.
		default:
			if err := refUpdater.Update(git.ReferenceName(status.Reference), status.NewOID, status.OldOID); err != nil {
				return false, false, fmt.Errorf("queueing ref to be updated: %w", err)
			}
			referencesUpdated = true

			// While scanning reference updates, check if any tags changed.
			if wereTagsChanged(status) {
				tagsChanged = true
			}

			// While scanning reference updates, check if repo was changed.
			if changeTypes[status.Type] {
				repoChanged = true
			}
		}
	}
	if scanner.Err() != nil {
		return false, false, fmt.Errorf("scanning fetch output: %w", scanner.Err())
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L227-231)
```go
	// Before committing the remaining reference updates, fetched objects must be migrated out of
	// the quarantine directory.
	if err := quarantineDir.Migrate(ctx); err != nil {
		return false, false, fmt.Errorf("migrating quarantined objects: %w", err)
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L233-236)
```go
	// Commit the remaining queued reference updates so the changes get applied.
	if err := refUpdater.Commit(); err != nil {
		return false, false, fmt.Errorf("committing reference update: %w", err)
	}
```

**File:** internal/git/quarantine/quarantine.go (L22-27)
```go
// Dir is a quarantine directory for Git objects. Instead of writing new commits into the main
// repository, they're instead written into a temporary quarantine directory. This staging area can
// either be migrated into the main repository or, alternatively, will automatically be discarded
// when the context gets cancelled. If the quarantine environment is discarded without being staged,
// then none of the objects which have been created in the quarantine directory will end up in the
// main repository.
```
