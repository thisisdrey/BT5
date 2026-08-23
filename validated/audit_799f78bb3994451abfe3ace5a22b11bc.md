### Title
Partial-success mishandling in `fetchRemoteAtomic`: pruned-ref deletions are durably committed before quarantine migration/ref-update, so a failure of the second phase is reported as total failure while the repository has already been mutated - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`fetchRemoteAtomic` splits a `FetchRemote` operation into two independent reference-update transactions plus a quarantine-object migration, exactly the kind of "split into sub-operations, then reduce to a single boolean/err" pattern that caused the ZeroLend bug. `prunedUpdater` (deleting stale refs) is `Prepare()`d and `Commit()`ed to completion *before* `quarantineDir.Migrate()` runs and before `refUpdater` (the remaining ref updates) is committed. If `Migrate()` or `refUpdater.Commit()` subsequently fails, the function returns only an error — there is no bookkeeping that reflects the fact that the pruned-ref deletions already happened and are irreversible. [1](#0-0) 

### Finding Description
The comment at lines 124-134 explains why two separate `updateref.Updater` instances are used (to avoid F/D ref conflicts): pruned refs must be committed in their own transaction ahead of the rest. [2](#0-1) 

The control flow is:
1. `prunedUpdater.Prepare()` / `prunedUpdater.Commit()` — this deletes local refs and is fully committed to disk (and casts votes via the reference-transaction hook, feeding Praefect's transaction manager / generation bookkeeping).
2. `refUpdater.Prepare()` — stages the remaining ref updates.
3. `quarantineDir.Migrate(ctx)` — moves fetched objects out of quarantine into the real object directory.
4. `refUpdater.Commit()` — applies the remaining ref updates. [1](#0-0) 

If step 3 or 4 fails (e.g. `finalizeObjectFile`/`os.Rename` failure due to disk pressure, permission error, or a crafted object name collision, or `refUpdater.Commit()` failing because required objects didn't make it out of quarantine), the function returns an error at line 230/235. At that point step 1 has already been durably applied: the repository's stale/pruned references are gone, but the intended new state (updated refs + migrated objects) was never applied. The function has no `all-or-nothing` semantics across the two sub-transactions — exactly analogous to `LendingPoolGauge.notifyRewardAmount()` returning `false` after `supplyGauge.notifyRewardAmount()` already succeeded and transferred funds: the caller's binary success/failure signal does not reflect that a real, unrecoverable side effect already occurred.

This is reachable by any caller of `FetchRemote` (used for pull mirroring) simply by making git-fetch prune some refs and then having the migrate/commit phase fail — which is plausible with a malicious/misbehaving remote that serves refs designed to prune existing branches while also sending a pack that triggers a migration failure (e.g. a colliding loose-object filename, or objects that exceed available disk causing `Migrate`'s `os.Rename`/sync to fail).

### Impact Explanation
- On a standalone Gitaly node, this can permanently delete local references (pruned) while the intended replacement state (new refs, migrated objects) never lands — an unrecoverable "freezing"/loss of repository state that also cannot be simply retried to a known-good state, since the RPC only reports a single terminal error and gives the caller no indication of which sub-phase partially succeeded.
- On a Praefect-fronted deployment, this is worse: `prunedUpdater.Commit()` drives the reference-transaction hook to cast votes/commit through the distributed transaction machinery, while the RPC as a whole ultimately errors out. Praefect's reconciliation logic (`getUpdatedAndOutdatedSecondaries`, `newRequestFinalizer`/`IncrementGeneration`) makes replication/generation decisions based on the aggregate RPC error and voting outcome, not on the fact that a durable partial mutation already happened on the primary. This class of "primary was dirtied but the caller/Praefect concludes total failure" divergence is precisely called out in Praefect's own logic as something that "cannot and should not fail" but is only a best-effort heuristic. [3](#0-2) 

### Likelihood Explanation
Triggering the migrate/commit failure window requires influencing an ordinary `FetchRemote` (pull-mirror) so that pruned refs are processed successfully while the pack/object migration or subsequent ref-update fails — reachable via a crafted/misbehaving remote URL that an ordinary user configures as a mirror source, with no privileged access to Gitaly needed. It requires a specific timing/failure condition (disk pressure, colliding filenames, or objects removed from quarantine failing to migrate), so it is not trivially reproducible on every fetch, but the code path is fully attacker-influenced (remote content controls which refs are pruned and what objects are sent).

### Recommendation
Make the two-phase pruned/refs + quarantine migration truly atomic, or track partial completion explicitly:
- Defer `prunedUpdater.Commit()` until after `quarantineDir.Migrate()` and `refUpdater.Prepare()` have both succeeded, committing both updaters back-to-back once the objects are safely migrated (mirroring the "prepare everything, then commit everything" pattern used elsewhere, e.g. `userSquash`'s vote-then-migrate-then-vote sequence). [4](#0-3) 

- If a strict two-phase split cannot be avoided (due to the F/D conflict constraint), record and surface which phase committed versus failed so that callers/Praefect can react correctly (e.g., always still emit a vote reflecting the actually-applied disk state, not "no vote" when only pruned refs landed), and ensure `Migrate()` failures are retried/reconciled rather than surfaced as a bare error with no state recovery.

### Proof of Concept
Not executable without live infrastructure, but the flow to reproduce conceptually:
1. Configure a mirror `FetchRemote` against a remote controlled by the attacker.
2. Have the remote's refs cause `git fetch --prune --dry-run --porcelain` to report at least one `RefUpdateTypePruned` entry for a ref that exists locally, so `prunedUpdater.Delete()` is queued and later `Prepare()`/`Commit()`ed successfully at lines 213-220.
3. Have the remote also send pack objects designed to make `quarantine.Migrate()`'s `finalizeObjectFile`/`os.Rename` fail (e.g., contention on the target loose-object path, or induced disk-full condition) so line 229 returns an error.
4. Observe: `FetchRemote` returns an internal error to the caller, yet the target ref has already been deleted (pruned) on disk — a partial, unreported mutation identical in kind to the ZeroLend `LendingPoolGauge` bug where one sub-operation's success is discarded by the aggregate failure signal.

### Citations

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

**File:** internal/gitaly/service/repository/fetch_remote.go (L212-236)
```go
	// Prepare pruned references in separate transaction to avoid F/D conflicts.
	if err := prunedUpdater.Prepare(); err != nil {
		return false, false, fmt.Errorf("preparing reference prune: %w", err)
	}

	// Commit pruned references to complete transaction and apply changes.
	if err := prunedUpdater.Commit(); err != nil {
		return false, false, fmt.Errorf("committing reference prune: %w", err)
	}

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

**File:** internal/praefect/coordinator.go (L964-982)
```go
// getUpdatedAndOutdatedSecondaries returns all nodes which can be considered up-to-date or outdated
// after the given transaction. A node is considered outdated, if one of the following is true:
//
//   - No subtransactions were created and the RPC was successful on the primary. This really is only
//     a safeguard in case the RPC wasn't aware of transactions and thus failed to correctly assert
//     its state matches across nodes. This is rather pessimistic, as it could also indicate that an
//     RPC simply didn't change anything. If the RPC was a failure on the primary and there were no
//     subtransactions, we assume no changes were done and that the nodes failed prior to voting.
//
//   - The node failed to be part of the quorum. As a special case, if the primary fails the vote, all
//     nodes need to get replication jobs.
//
//   - The node has a different error state than the primary. If both primary and secondary have
//     returned the same error, then we assume they did the same thing and failed in the same
//     controlled way.
//
// Note that this function cannot and should not fail: if anything goes wrong, we need to create
// replication jobs to repair state.
func getUpdatedAndOutdatedSecondaries(
```

**File:** internal/gitaly/service/operations/squash.go (L190-210)
```go
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
```
