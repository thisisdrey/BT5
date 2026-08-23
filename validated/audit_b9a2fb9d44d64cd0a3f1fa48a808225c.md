### Title
Permanent repository freeze from unrecoverable cached migration failure state - ([File: internal/gitaly/storage/storagemgr/partition/migration/manager.go])

### Summary
`migrationManager.migrate()` gates every `Begin()` call (i.e. every read/write transaction, including those started by ordinary push/fetch RPCs) behind a per-repository migration check. If the one-time migration attempt for a repository fails, the failure is cached forever in an in-memory map with no expiry, retry, or cleanup path, permanently blocking all future transactions against that repository for the remaining lifetime of the storage process.

### Finding Description
`migrationManager.Begin` unconditionally calls `m.migrate(ctx, opts)` before delegating to the wrapped `Partition.Begin` [1](#0-0) . Inside `migrate`, the first goroutine to touch a given `relativePath` creates a `migrationState{doneCh: doneCh}` and stores it in the un-expiring `m.migrationStates` map, then unlocks the mutex and proceeds to run `performMigrations` [2](#0-1) .

Any concurrent or later call for the same `relativePath` finds the cached state (`ok == true`) and simply waits on `state.doneCh`; once closed, it returns whatever `state.err` was recorded, without re-attempting the migration [3](#0-2) .

If `performMigrations` fails for any reason (e.g. a transient error from `txn.Commit`, a conflict, or an I/O failure while writing the migration key), the error is recorded on `state.err`, `doneCh` is closed via the deferred `close(doneCh)`, and the entry is never removed from `migrationStates` and never retried [4](#0-3) . There is no TTL, no explicit deletion of the map entry, and no code path that clears `state.err` and re-drives migration on a subsequent request — the map is only ever inserted into, never invalidated.

This is analogous to the reported "reentrant / improperly gated state machine" bug class: a state machine (here, "has this repository's migration completed?") can be driven into a terminal failure state by a single failed attempt, and there is no way for the system to recover from it — every subsequent legitimate operation is rejected with the same stale error forever, freezing the resource (the repository) rather than the isolated failed operation.

### Impact Explanation
Once a repository's one-time migration attempt fails, `migrationManager.Begin` returns `fmt.Errorf("migrate: %w", err)` for that repository on every subsequent call for the lifetime of the process [5](#0-4) . Since `Begin` is the single entry point for both read and write transactions on a partition, this permanently denies service to the affected repository — no push, fetch, or read RPC that needs to open a transaction against it can succeed until the process is restarted (which resets the in-memory `migrationStates` map). This is a full, indefinite freeze of access to a specific repository triggered by a single failed migration run, with no self-healing or administrative recovery path visible in this component.

### Likelihood Explanation
Migrations run once per repository on the first `Begin()` after new migrations are registered [6](#0-5) , and `performMigrations` itself opens a nested write transaction via `m.Partition.Begin` and commits it [7](#0-6) , meaning any transient contention, disk pressure, or conflict during that inner transaction (conditions an ordinary client's normal traffic pattern can plausibly induce, since the migration is triggered by that same client's request) can cause the failure to be latched in permanently. The bug requires no special privilege — any client whose ordinary push/fetch happens to be the one that triggers the first migration attempt on a repository can end up freezing it if that attempt fails for any transient reason.

### Recommendation
- Do not cache failed migration state indefinitely; either remove the failed `migrationState` entry from `migrationStates` after logging/reporting the error, or add a bounded retry/backoff so future `Begin()` calls re-attempt the migration rather than repeating a stale error forever.
- Guard writes to `state.err` and reads of it with the same mutex used for map access, and ensure the failure path is explicitly reversible (e.g., an administrative or automatic retry mechanism) so a single transient failure cannot become a permanent DoS.

### Proof of Concept
1. Configure the storage with at least one repository migration in `migrations`.
2. Trigger any RPC that calls `Begin()` for a given repository (`relativePath`) such that `performMigrations` is invoked for the first time — for example, ensure the nested write transaction inside `performMigrations` fails once (e.g., due to a WAL/commit conflict or transient I/O error) [8](#0-7) .
3. Observe that `migrationStates[relativePath]` now holds a `migrationState` with `err` set and a closed `doneCh`.
4. Issue any further RPC against the same repository (push, fetch, read) — every call to `migrationManager.Begin` immediately returns the cached `"migrate: waiting on migrations: ..."` error without ever re-attempting the migration, for as long as the process runs [3](#0-2) .

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L66-72)
```go
func (m *migrationManager) Begin(ctx context.Context, opts storage.BeginOptions) (storage.Transaction, error) {
	if err := m.migrate(ctx, opts); err != nil {
		return nil, fmt.Errorf("migrate: %w", err)
	}

	return m.Partition.Begin(ctx, opts)
}
```

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L79-99)
```go
// migrate handles setting up migration state and executing outstanding migrations.
func (m *migrationManager) migrate(ctx context.Context, opts storage.BeginOptions) error {
	relativePaths := opts.RelativePaths
	// To perform a migration, the manager must have migrations configured and the transaction must
	// target a repository. If not, skip migration handling and proceed with the transaction.
	if m.migrations == nil || len(*m.migrations) == 0 || len(relativePaths) == 0 {
		return nil
	}

	relativePath := relativePaths[0]

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
```

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L101-113)
```go
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

**File:** internal/gitaly/storage/storagemgr/partition/migration/manager.go (L123-129)
```go
	if err := m.performMigrations(mCtx, opts); err != nil {
		// Record the error as part of the migration state so concurrent transactions are notified.
		state.err = err
		return fmt.Errorf("performing migrations: %w", err)
	}

	return nil
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
