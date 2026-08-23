### Title
Permanent per-repository DoS from a single cached migration failure - ([File: internal/gitaly/storage/storagemgr/partition/migration/manager.go])

### Summary
`migrationManager.migrate` gates every `Begin()` call for a repository behind an in-memory, per-`relativePath` `migrationState` that is created once and never cleared. If the underlying migration transaction fails for any reason, the error is cached in that state forever, and every subsequent transaction attempt against the same repository — i.e., every future git operation from any ordinary user — immediately fails with the same cached error, with no retry path short of a Gitaly process restart. This mirrors the reported bug class: a two-step operation (register state → perform migration) where the first step always "succeeds" and marks a definitive status, but the second step can fail due to a transient or environmental condition, leaving the recorded state permanently stuck and halting all further activity that depends on it.

### Finding Description
`migrate` first registers a `migrationState` for the repository's `relativePath` in `m.migrationStates` under `m.mu`, then calls `performMigrations`. If a concurrent/subsequent `Begin()` finds an existing state, it waits on `state.doneCh` and, once closed, returns `state.err` if set: [1](#0-0) 

The state is registered before the migration transaction runs, and `state.err` is set on any failure from `performMigrations` — including transient failures such as a failed `Begin` on the underlying partition, a failed `migration.run`, a failed `recordID` (KV `Set`), or a failed `Commit`: [2](#0-1) 

`performMigrations` itself opens a write transaction, runs each pending `Migration.Fn`, and commits; any error at `migration.run`, `migration.recordID`, or `txn.Commit` bubbles straight back to `migrate`, and the transaction is rolled back — but the cached `migrationState.err` in the map is left intact: [3](#0-2) 

Critically, `doneCh` is closed via `defer close(doneCh)` at registration time (line 95), and nothing in the code path ever deletes the entry from `m.migrationStates` or resets `state.err`. Once one transaction hits `performMigrations` and fails — for any reason, including transient disk, KV-store, or WAL errors that are unrelated to the actual migration logic — every future `Begin()` call routed through `migrationManager.Begin` for that `relativePath` takes the `ok == true` branch, observes `state.err != nil`, and immediately returns `"waiting on migrations: %w"` without ever attempting the migration again: [4](#0-3) 

Because `migrationManager` wraps `storagemgr.Partition.Begin`, which is the entry point used for every read/write transaction (pushes, fetches, hook invocations, housekeeping, etc.), this permanently blocks all Gitaly traffic to that specific repository until the process restarts (the map is only initialized fresh in `newPartition`, called at partition/storage startup): [5](#0-4) 

### Impact Explanation
This is a concrete, self-inflicted DoS of a core RPC-handler resource (repository transaction admission) triggered by an ordinary condition — a single failed migration attempt (e.g., a transient KV commit error, disk pressure during a WAL commit, or any bug in a registered `Migration.Fn`) — that then wedges every subsequent user-facing operation against that repository (push, fetch, clone, any RPC that calls `Begin`). Unlike a legitimate "vault paused" scenario that can be resolved by a privileged actor toggling a flag, here there is no in-process recovery mechanism at all; the only way to clear the stuck state is to restart the Gitaly node (which reinitializes `migrationStates` from scratch), which is an operational/administrative action far outside the reach of the requesting client and disruptive to the whole node, not just the affected repository.

### Likelihood Explanation
Migrations are opt-in configuration (`m.migrations`), but the failure path only requires a single transient error inside `performMigrations` (KV write conflict, transaction commit failure, disk error, or any error surfaced by a registered `Migration.Fn`) to occur once for a given repository while migrations are enabled. Given migrations run automatically and silently on the first `Begin()` for any repository after upgrade/rollout, and Gitaly transaction commits can legitimately fail under load or resource contention, the likelihood of hitting this stuck state in production is non-trivial once any migration is configured, and its blast radius (permanent denial of all traffic to the affected repository) is high relative to the low effort required to trigger it.

### Recommendation
Do not cache migration failures indefinitely in `migrationManager.migrate`. On `performMigrations` failure, remove the entry from `m.migrationStates` (or otherwise reset it) so a subsequent `Begin()` retries the migration rather than short-circuiting on a stale cached error, e.g. by deleting `m.migrationStates[relativePath]` under `m.mu` in the error branch at manager.go lines 123-127, or by only marking success permanently while treating failures as retryable per-attempt state.

### Proof of Concept
1. Configure a `Migration` (or trigger any real migration in the codebase) for a partition/storage.
2. Cause `performMigrations` to fail once for a specific repository — e.g., inject a failure into `migration.run`'s `Fn`, or force `txn.Commit` to fail (disk full, forced KV conflict) during the migration transaction at manager.go lines 197-213.
3. Observe that `migrate` returns `"performing migrations: ..."` and `state.err` is now set in `m.migrationStates[relativePath]`, with `doneCh` already closed (manager.go lines 92-99, 123-126).
4. Issue any subsequent RPC against the same repository (e.g., a normal `fetch`/`push` that calls `Begin`); observe it immediately fails with `"migrate: waiting on migrations: ..."` (manager.go lines 66-71, 101-112) even though the underlying disk/KV issue that caused the original failure may have been transient and resolved.
5. The repository remains permanently unreachable through this partition until the Gitaly process hosting it is restarted, since `migrationStates` is only reset in `newPartition` (manager.go lines 50-64).

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L50-64)
```go
// newPartition creates a migration manager that wraps the provided partition.
func newPartition(partition storagemgr.Partition, logger log.Logger, metrics Metrics, storageName string, migrations *[]Migration) storagemgr.Partition {
	ctx, cancel := context.WithCancel(context.Background())

	return &migrationManager{
		ctx:             ctx,
		cancelFn:        cancel,
		Partition:       partition,
		logger:          logger,
		metrics:         metrics,
		storageName:     storageName,
		migrations:      migrations,
		migrationStates: map[string]*migrationState{},
	}
}
```

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L66-72)
```go
func (m *migrationManager) Begin(ctx context.Context, opts storage.BeginOptions) (storage.Transaction, error) {
	if err := m.migrate(ctx, opts); err != nil {
		return nil, fmt.Errorf("migrate: %w", err)
	}

	return m.Partition.Begin(ctx, opts)
}
```

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L90-113)
```go
	// Check if the repository already has a pending migration.
	m.mu.Lock()
	state, ok := m.migrationStates[relativePath]
	if !ok {
		doneCh := make(chan struct{})
		defer close(doneCh)
		state = &migrationState{doneCh: doneCh}
		m.migrationStates[relativePath] = state
	}
	m.mu.Unlock()

	// Block concurrent transactions on the same repository until outstanding migrations complete.
	if ok {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-state.doneCh:
			if state.err != nil {
				// Migrations are required to succeed before the repository can serve traffic.
				return fmt.Errorf("waiting on migrations: %w", state.err)
			}
			return nil
		}
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L115-130)
```go
	// Capture the metadata from the request's context and append it to the manager's context.
	// This allows us to use feature flags with the manager's context too.
	mCtx := m.ctx
	md, ok := metadata.FromIncomingContext(ctx)
	if ok {
		mCtx = metadata.NewIncomingContext(mCtx, md)
	}

	if err := m.performMigrations(mCtx, opts); err != nil {
		// Record the error as part of the migration state so concurrent transactions are notified.
		state.err = err
		return fmt.Errorf("performing migrations: %w", err)
	}

	return nil
}
```

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L154-219)
```go
	// Start a single transaction that records all outstanding migrations that get executed.
	txn, err := m.Partition.Begin(ctx, storage.BeginOptions{
		Write:                            true,
		RelativePaths:                    opts.RelativePaths,
		SkipPreventingReftableCompaction: true,
	})
	if err != nil {
		return fmt.Errorf("begin migration update: %w", err)
	}
	defer func() {
		if returnedErr != nil {
			if err := txn.Rollback(ctx); err != nil {
				returnedErr = errors.Join(err, fmt.Errorf("rollback: %w", err))
			}
		}
	}()

	for _, migration := range *m.migrations {
		timer := prometheus.NewTimer(m.metrics.latencyMetric.With(prometheus.Labels{
			"migration_name": migration.Name,
		}))

		if id >= migration.ID {
			continue
		}

		logger := m.logger.WithFields(log.Fields{
			"migration_name": migration.Name,
			"migration_id":   migration.ID,
			"relative_path":  relativePath,
		})

		// A migration may have configuration allowing it to be disabled. As migrations are
		// performed in order, if a disabled migration is encountered, the remaining migrations are
		// also not executed. Since repository migrations are currently only attempted once for a
		// repository during the partition lifetime, a previously disabled migration may not
		// immediately be executed in the next transaction. Migration state must first be reset.
		if migration.IsDisabled != nil && migration.IsDisabled(ctx) {
			break
		}

		logger.Info("running migration")

		if err := migration.run(ctx, txn, m.storageName, relativePath); err != nil {
			return fmt.Errorf("run migration: %w", err)
		}

		// If migration operations are successfully recorded, the last run migration ID is also recorded
		// signifying it has been completed.
		if err := migration.recordID(txn, relativePath); err != nil {
			return fmt.Errorf("setting migration key: %w", err)
		}

		duration := timer.ObserveDuration()
		logger.WithField("duration", duration).Info("migration successful")
	}

	commitLSN, err := txn.Commit(ctx)
	if err != nil {
		return fmt.Errorf("commit migration update: %w", err)
	}

	storage.LogTransactionCommit(ctx, m.logger, commitLSN, "migrator")

	return nil
}
```
