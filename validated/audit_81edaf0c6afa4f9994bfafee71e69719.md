### Title
Attacker-controlled `FetchRemoteRequest.Timeout` can abort `FetchRemote` between its two non-atomic reference-update transactions, permanently pruning refs while leaving new refs uncommitted - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`FetchRemote` lets the caller supply an arbitrary, unbounded `Timeout` value that becomes the context deadline for the entire RPC. The RPC's reference-update logic is split into two **separate, non-atomic** `updateref.Updater` transactions (one for pruned refs, one for all other updates), with a quarantine-object migration in between. If the context expires after the first transaction commits but before the second one commits, the repository is left in a permanently inconsistent state - deleted references are gone forever, but their replacements were never written.

### Finding Description
`FetchRemote` derives the RPC's context deadline directly from the untrusted `req.GetTimeout()` field, with no minimum enforced: [1](#0-0) 

`validateFetchRemoteRequest` never checks this value: [2](#0-1) 

Inside `fetchRemoteAtomic`, the code explicitly acknowledges that pruned-reference updates and regular reference updates are committed via **two separate transactions** to work around F/D-conflict limitations of `git-update-ref`, meaning they are not applied atomically with respect to each other: [3](#0-2) 

The pruned-reference transaction is prepared and **committed first**: [4](#0-3) 

Only afterward is the main reference-update transaction prepared, the quarantined objects migrated, and finally committed: [5](#0-4) 

Because the RPC's `ctx` (derived from the attacker-supplied `Timeout`) is threaded through every one of these steps (`prunedUpdater.Commit`, `quarantineDir.Migrate`, `refUpdater.Prepare/Commit`), a caller can pick a `Timeout` value calibrated to expire exactly after `prunedUpdater.Commit()` succeeds but before `refUpdater.Commit()` (or even `quarantineDir.Migrate()`) completes. This mirrors the reported bug class: a caller-controlled resource/time-limit parameter with no lower bound, whose deliberate under-provisioning causes an operation that already partially executed irreversible side effects (ref deletion) to fail before completing its counterpart update, leaving state that cannot self-heal and needs out-of-band repair (re-fetch/fsck/manual ref recreation) rather than any built-in retry/replay path.

### Impact Explanation
A successful timing attack leaves the target repository with:
- References that existed on the remote and were slated for pruning **permanently deleted** on the local mirror.
- The corresponding new/updated references from the same fetch **never applied**, because `refUpdater.Commit()` never ran or was interrupted mid-migration.

This produces a corrupted mirror/import state (missing branches/tags, or objects staged in a partially-migrated quarantine) with no automatic recovery mechanism in the RPC itself - the operator must intervene (re-run the fetch, run consistency checks, or manually restore refs) to repair the repository. This is a concrete DoS of the `FetchRemote` handler's atomicity guarantee that requires privileged/administrative remediation, analogous to the original report's requirement for governance action to recover stuck funds.

### Likelihood Explanation
`FetchRemote` is invoked whenever GitLab performs pull mirroring or repository import, and the `Timeout` field is part of the protobuf request that reaches Gitaly directly. Any caller able to configure a fetch/mirror/import operation (or invoke the RPC directly) controls this value, and no minimum is enforced by `validateFetchRemoteRequest`. The precise window in which `prunedUpdater.Commit()` has completed but `refUpdater.Commit()` has not is timing-dependent, but is a deterministically reachable race that can be tuned by choosing `Timeout` relative to observed fetch/prune sizes (e.g., forcing many refs to be pruned to widen the window between the two commits), making repeated exploitation practical.

### Recommendation
- Enforce a sane minimum (and possibly maximum) for `FetchRemoteRequest.Timeout` in `validateFetchRemoteRequest`, rejecting values too small to reasonably complete a fetch.
- Make the pruned-reference transaction and the main reference-update transaction atomic with respect to cancellation - e.g., only commit pruned deletions after the main transaction has also been prepared/committed successfully, or roll back the pruned commit if the main transaction subsequently fails/times out.
- Alternatively, detach the deadline from prepare/commit of already-in-flight reference transactions so that once the first transaction commits, the RPC completes the whole atomic unit rather than aborting between transactions.

### Proof of Concept
1. Configure a pull mirror/import (or call `FetchRemote` directly) against a remote that will cause a number of local references to be pruned and a number of new/updated references to be applied in the same fetch.
2. Set `FetchRemoteRequest.Timeout` to a small value calibrated (via prior observation of fetch/prune timings) to expire once `prunedUpdater.Commit()` in `fetchRemoteAtomic` has completed but before `refUpdater.Commit()` (or `quarantineDir.Migrate`) finishes.
3. Observe that the RPC fails with a context-deadline error, yet the pruned references are permanently removed from the repository while the new/updated references are missing, leaving the repository in a broken, inconsistent state that must be manually repaired.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L35-39)
```go
	if req.GetTimeout() > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, time.Duration(req.GetTimeout())*time.Second)
		defer cancel()
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L124-154)
```go
	// A repository cannot contain references with F/D (file/directory) conflicts (i.e.
	// `refs/heads/foo` and `refs/heads/foo/bar`). If fetching from the remote repository
	// results in an F/D conflict, the reference update fails. In some cases a conflicting
	// reference may exist locally that does not exist on the remote. In this scenario, if
	// outdated references are first pruned locally, the F/D conflict can be avoided. When
	// `git-fetch(1)` is performed with the `--prune` and `--dry-run` flags, the pruned
	// references are also included in the output without performing any actual reference
	// updates. Bulk atomic reference updates performed by `git-update-ref(1)` do not support
	// F/D conflicts even if the conflicted reference is being pruned. Therefore, pruned
	// references must be updated first in a separate transaction. To accommodate this, two
	// different instances of `updateref.Updater` are used to keep the transactions separate.
	prunedUpdater, err := updateref.New(ctx, quarantineRepo)
	if err != nil {
		return false, false, fmt.Errorf("spawning pruned updater: %w", err)
	}
	defer func() {
		if err := prunedUpdater.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("cancel pruned updater: %w", err)
		}
	}()

	// All other reference updates can be queued as part of the same transaction.
	refUpdater, err := updateref.New(ctx, quarantineRepo)
	if err != nil {
		return false, false, fmt.Errorf("spawning ref updater: %w", err)
	}
	defer func() {
		if err := refUpdater.Close(); err != nil && returnedErr == nil {
			returnedErr = fmt.Errorf("cancel ref updater: %w", err)
		}
	}()
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L212-220)
```go
	// Prepare pruned references in separate transaction to avoid F/D conflicts.
	if err := prunedUpdater.Prepare(); err != nil {
		return false, false, fmt.Errorf("preparing reference prune: %w", err)
	}

	// Commit pruned references to complete transaction and apply changes.
	if err := prunedUpdater.Commit(); err != nil {
		return false, false, fmt.Errorf("committing reference prune: %w", err)
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L222-236)
```go
	// Prepare the remaining queued reference updates.
	if err := refUpdater.Prepare(); err != nil {
		return false, false, fmt.Errorf("preparing reference update: %w", err)
	}

	// Before committing the remaining reference updates, fetched objects must be migrated out of
	// the quarantine directory.
	if err := quarantineDir.Migrate(ctx); err != nil {
		return false, false, fmt.Errorf("migrating quarantined objects: %w", err)
	}

	// Commit the remaining queued reference updates so the changes get applied.
	if err := refUpdater.Commit(); err != nil {
		return false, false, fmt.Errorf("committing reference update: %w", err)
	}
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L306-320)
```go
func (s *server) validateFetchRemoteRequest(ctx context.Context, req *gitalypb.FetchRemoteRequest) error {
	if err := s.locator.ValidateRepository(ctx, req.GetRepository()); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	if req.GetRemoteParams() == nil {
		return structerr.NewInvalidArgument("missing remote params")
	}

	if req.GetRemoteParams().GetUrl() == "" {
		return structerr.NewInvalidArgument("blank or empty remote URL")
	}

	return nil
}
```
