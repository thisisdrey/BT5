### Title
`FetchRemote`'s two-phase reference update is not atomic despite being named `fetchRemoteAtomic`, allowing partial application (pruned-ref deletion without corresponding updates) on failure - ([File: internal/gitaly/service/repository/fetch_remote.go])

### Summary
`fetchRemoteAtomic` splits reference updates from a fetch into two separate `updateref.Updater` transactions — one for pruned (deleted) references and one for all other ref creates/updates/fast-forwards. Because Git's `git-update-ref` bulk transaction cannot express an F/D-conflicting delete+create in a single atomic transaction, the code deliberately commits the deletions first, then afterwards tries to migrate quarantined objects and commit the remaining updates. If anything after the first `Commit()` fails, the function returns an error, but the pruned-reference deletions committed in the first phase are never rolled back.

### Finding Description
The function is documented and named as if it performs an atomic fetch: [1](#0-0) 

It sets up an object quarantine so fetched objects are only migrated into the main object directory right before the final commit, which is the correct half of the atomicity story: [2](#0-1) 

However, reference updates are deliberately split into two independent transactions because of an F/D-conflict limitation in `git-update-ref(1)`: [3](#0-2) 

The pruned-ref transaction is prepared and **committed to the real repository** first: [4](#0-3) 

Only afterwards does the code prepare the remaining updates, migrate the quarantined objects, and commit the second transaction: [5](#0-4) 

If `refUpdater.Prepare()` fails (e.g. lock contention, disk I/O error), `quarantineDir.Migrate(ctx)` fails (e.g. disk full, permission error, crash), or `refUpdater.Commit()` fails, the function returns an error at lines 224, 230, or 235 — but the pruned references were already deleted and committed at line 218 and are **not restored**. The two transactions are independent `git-update-ref` invocations; there is no wrapping transaction or compensating rollback that undoes the first commit if the second one fails. The repository is left in an inconsistent state: branches/tags that existed upstream (or existed only locally and were meant to be pruned together with new content arriving) are deleted, while the corresponding new commits/tags that should accompany that prune never get applied and the newly fetched objects may or may not be present depending on where exactly the failure occurred.

This is directly analogous to the reported ERC4626 issue: a function whose contract/name promises an atomic, all-or-nothing outcome ("must revert if not all can be completed") instead performs a partial state change and returns an error without reverting the already-applied portion.

### Impact Explanation
`FetchRemote` is the RPC that backs GitLab's remote/pull-mirroring and repository import-by-URL features, both of which are triggered by ordinary, unprivileged project-level actions (configuring a pull mirror or importing from a URL) rather than by any privileged Gitaly operator action. A failure injected or naturally occurring between the two `Commit()` calls (disk pressure, transient I/O error, request timeout/cancellation via `req.GetTimeout()`, or the remote connection dropping mid-way through the fetch) causes:
- Permanent, unrecoverable loss of local references (branches/tags removed by the prune) with no compensating fetch of the corresponding upstream state.
- Inconsistent repository state that is reported to the caller as a failed RPC even though a real, unrolled-back mutation occurred, violating the caller's expectation that a failed "atomic" fetch leaves the repository untouched.
- With `DisableTransactions: true` (line 63) explicitly used for this RPC, Gitaly's own transaction/voting layer does not protect the two Git-level `update-ref` transactions from being partially applied across cluster replicas either, so the divergence between the two phases can also produce cross-replica drift in a Praefect-fronted deployment.

### Likelihood Explanation
No malicious remote or privileged actor is required — a plain transient failure (disk error, I/O contention, RPC timeout enforced by `req.GetTimeout()`, context cancellation from the client, or an interrupted quarantine migration) occurring in the narrow window between the two `Commit()` calls is sufficient to trigger the partial state. Any repository that both prunes remote-deleted refs and receives other ref updates in the same fetch (a common, realistic mirroring scenario) exercises this two-phase path on every sync.

### Recommendation
Make `fetchRemoteAtomic` genuinely atomic with respect to its own name/contract:
- Defer committing the pruned-reference transaction until after the second transaction has been prepared and the quarantine has been successfully migrated, committing both only once all preconditions for success are known, or
- If a true single bulk atomic `git-update-ref` transaction covering both pruned and non-pruned refs cannot be constructed due to the F/D-conflict limitation, add explicit compensation logic that restores the pruned references if any later step (`refUpdater.Prepare`, `quarantineDir.Migrate`, `refUpdater.Commit`) fails, and only report success to the caller once the full set of changes (or none of them) has been durably applied.
- At minimum, clearly document (and surface via a distinct error/response field) that a failure after `prunedUpdater.Commit()` leaves prune-only changes applied, so callers/operators can detect and reconcile the resulting inconsistent state instead of assuming the fetch fully reverted.

### Proof of Concept
1. Set up a repository `R` with a local branch `refs/heads/stale` that no longer exists on `remote`, and configure a `FetchRemoteRequest` with `Prune: true` (default, since `NoPrune` is false) so `refs/heads/stale` will be pruned, along with new commits on `refs/heads/main` that must be fetched.
2. Call `FetchRemote`. Internally:
   - `quarantineRepo.FetchRemote` runs the dry-run fetch and reports `stale` as pruned and `main` as updated.
   - `prunedUpdater` queues deletion of `refs/heads/stale`; `refUpdater` queues the update of `refs/heads/main`.
   - `prunedUpdater.Prepare()` and `prunedUpdater.Commit()` succeed — `refs/heads/stale` is now deleted from the real repository (line 218).
3. Before `refUpdater.Commit()` completes, inject a failure in the window between lines 220–235 (e.g., cause `quarantineDir.Migrate(ctx)` to fail by making the target object directory temporarily unwritable, or cancel the RPC context so `refUpdater.Prepare()`/`Commit()` returns an error).
4. `FetchRemote` returns a non-nil error to the caller (e.g. `"migrating quarantined objects: ..."` or `"committing reference update: ..."`).
5. Inspect the repository: `refs/heads/stale` is permanently gone, but `refs/heads/main` was never updated with the new commits that should have accompanied the prune — the fetch is reported as failed, yet the repository state has irreversibly changed, violating the "atomic" contract implied by `fetchRemoteAtomic`'s name and comments.

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L49-51)
```go
// fetchRemoteAtomic fetches changes from the specified remote repository. To be atomic, fetched
// objects are first quarantined and only migrated before committing the reference transaction.
func (s *server) fetchRemoteAtomic(ctx context.Context, req *gitalypb.FetchRemoteRequest) (_ bool, _ bool, returnedErr error) {
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L91-101)
```go
	// When performing fetch, objects are received before references are updated. If references fail
	// to be updated, unreachable objects could be left in the repository that would need to be
	// garbage collected. To be more atomic, a quarantine directory is set up where objects will be
	// fetched prior to being migrated to the main repository when reference updates are committed.
	quarantineDir, quarantineCleanup, err := quarantine.New(ctx, req.GetRepository(), s.logger, s.locator)
	if err != nil {
		return false, false, fmt.Errorf("creating quarantine directory: %w", err)
	}
	defer func() {
		quarantineCleanup() // Errors are logged by the tempdir package
	}()
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
