### Title
Downgrade-protection bypass via unrecorded generation lets replication silently overwrite/downgrade a target repository - ([File: internal/praefect/datastore/repository_store.go])

### Summary
The bug class in the report is: a state-transition guard checks only a time/threshold comparison and forgets to also gate on the "not yet initialized" (zero) value of the state being compared, letting an unprivileged caller drive the system into the guarded action without ever satisfying the intended precondition. In Gitaly's Praefect, the analogous guard is `PostgresRepositoryStore.GetReplicatedGeneration`, which is meant to prevent a replication job from ever moving a repository replica backwards to an older generation. It only enforces that protection `if targetGeneration != GenerationUnknown`, i.e. it is unconditionally skipped whenever the target has no recorded generation.

### Finding Description
`GetReplicatedGeneration` computes `sourceGeneration`/`targetGeneration` from the `storage_repositories` table and only raises `DowngradeAttemptedError` when `targetGeneration != GenerationUnknown && targetGeneration >= sourceGeneration`: [1](#0-0) 

`GenerationUnknown` (`-1`) is the sentinel used whenever a storage has no row for the repository in `storage_repositories`: [2](#0-1) 

This mirrors the Vault bug precisely: `proposedFeeTime == 0` (never proposed) was treated as satisfying the quit-period elapsed check; here `targetGeneration == GenerationUnknown` (never recorded) is treated as satisfying the "no active/greater version to protect" precondition, so the downgrade check is skipped outright rather than being enforced against `-1`.

The design doc explicitly documents this as accepted behavior for old, pre-generation-tracking clusters: [3](#0-2) 

However, "no recorded generation" is not exclusively a legacy-cluster condition — any storage entry that currently has no `storage_repositories` row (e.g. following `DeleteReplica`/`DeleteInvalidRepository`, a partially-completed repository creation, or a race between concurrent replication/removal operations) will silently accept a job whose `sourceGeneration` may be `GenerationUnknown` as well (both `-1`), or otherwise arbitrarily old, because the comparison `targetGeneration >= sourceGeneration` is never evaluated. The consumer, `defaultReplicator.Replicate`, unconditionally calls `ReplicateRepository` and then commits whatever generation it received: [4](#0-3) [5](#0-4) 

### Impact Explanation
When the guard is bypassed, `ReplicateRepository` (an RPC reachable from ordinary replication workflows, not a privileged administrative path) is executed against the target with no verification that the incoming data is not stale relative to whatever the target may already contain on disk (the DB metadata simply doesn't know about it). This can result in a target repository being overwritten with older or otherwise inconsistent Git object state, i.e., a form of cross-replica data corruption/downgrade that the whole generation-tracking mechanism exists specifically to prevent. Because `storage_repositories` rows can be removed via normal deletion/invalid-repository cleanup flows that ordinary repository lifecycle operations (create, remove, push-driven repair) can trigger, an attacker who can race repository deletion/creation and pushes on a given relative path may be able to force the target into the "unrecorded generation" state and then have a stale replication job land undetected, defeating Praefect's replica-consistency guarantee.

### Likelihood Explanation
Exploitation requires winning a race between repository lifecycle operations (deletion, invalid-repository cleanup, or fresh assignment) and an in-flight or newly scheduled replication job for the same repository — this is a narrower, timing-dependent condition compared to the original Vault bug (which is trivially exercisable by anyone at any time). It does not require any elevated privilege, credentials, or a malicious peer; it is reachable purely through the ordinary create/push/delete/replicate lifecycle of a repository. Likelihood is assessed as Medium: concrete and reachable, but requires a race window rather than being unconditionally exploitable.

### Recommendation
Do not treat `GenerationUnknown` on the target as an automatic pass. Instead:
- Only allow replication into a target with `GenerationUnknown` when the source is verified to be the authoritative/only known copy (e.g., cross-check `repository_assignments`/`repositories` table state, not just the two storage rows), or
- Require an explicit "first replica" marker set only at initial repository creation, and reject/queue-for-review any replication job that lands on a target whose record disappeared after having previously existed (e.g. track a tombstone or last-known generation instead of deleting the row outright), so the comparison can still be made against the last known value rather than being skipped.

### Proof of Concept
Conceptual repro (matches the existing test harness in `repository_store_test.go`):
```go
// 1. Repository initially created and replicated normally; target has generation 1.
rs.CreateRepository(ctx, id, vs, repo, replicaPath, "target", nil, nil, false, false)
rs.IncrementGeneration(ctx, id, "target", nil) // target generation = 1

// 2. Target's storage_repositories row is removed (e.g. via DeleteReplica/DeleteInvalidRepository
//    triggered by a concurrent repository-lifecycle operation), even though the on-disk repo
//    on "target" may still exist with generation-1 (or newer) data.
rs.DeleteReplica(ctx, id, "target")

// 3. A stale/old replication job (source generation 0, or itself GenerationUnknown) is applied.
gen, err := rs.GetReplicatedGeneration(ctx, id, "source", "target")
// err == nil, no DowngradeAttemptedError raised, even though "target" previously
// held a newer generation than what "source" is about to overwrite it with.
```
This is analogous to the confirmed test case `"no previous record allowed"` in `repository_store_test.go`, which documents that `GetReplicatedGeneration` returns success with no error when the target has no record — demonstrating the same "unset state satisfies the gate" pattern as the reported Vault `changeFees` bug: [6](#0-5)

### Citations

**File:** internal/praefect/datastore/repository_store.go (L16-21)
```go
type storages map[string][]string

// GenerationUnknown is used to indicate lack of generation number in
// a replication job. Older instances can produce replication jobs
// without a generation number.
const GenerationUnknown = -1
```

**File:** internal/praefect/datastore/repository_store.go (L339-368)
```go
	sourceGeneration := GenerationUnknown
	targetGeneration := GenerationUnknown
	for rows.Next() {
		var storage string
		var generation int
		if err := rows.Scan(&storage, &generation); err != nil {
			return 0, err
		}

		switch storage {
		case source:
			sourceGeneration = generation
		case target:
			targetGeneration = generation
		default:
			return 0, fmt.Errorf("unexpected storage: %s", storage)
		}
	}

	if err := rows.Err(); err != nil {
		return 0, err
	}

	if targetGeneration != GenerationUnknown && targetGeneration >= sourceGeneration {
		return 0, DowngradeAttemptedError{
			Storage:             target,
			CurrentGeneration:   targetGeneration,
			AttemptedGeneration: sourceGeneration,
		}
	}
```

**File:** doc/virtual_storage.md (L66-66)
```markdown
**Note:** Praefect only enforces the downgrade protection if the target repository has a recorded generation. If the target or both source and the target do not have recorded generations, the replication job is allowed go through as Praefect does not know the state of the repositories. This behavior is allowed as a cluster prior to repository generations will not have a record for a given repository but might produce replication jobs. An upgraded cluster should never produce a replication job for a repository that does not have a generation record. This behavior can be disabled once migration is performed as described in [#3003](https://gitlab.com/gitlab-org/gitaly/-/issues/3033).
```

**File:** internal/praefect/replicator.go (L57-73)
```go
	generation, err := dr.rs.GetReplicatedGeneration(ctx, event.Job.RepositoryID, event.Job.SourceNodeStorage, event.Job.TargetNodeStorage)
	if err != nil {
		// Later generation might have already been replicated by an earlier replication job. If that's the case,
		// we'll simply acknowledge the job. This also prevents accidental downgrades from happening.
		var downgradeErr datastore.DowngradeAttemptedError
		if errors.As(err, &downgradeErr) {
			message := "repository downgrade prevented"
			if downgradeErr.CurrentGeneration == downgradeErr.AttemptedGeneration {
				message = "target repository already on the same generation, skipping replication job"
			}

			logger.WithError(downgradeErr).Info(message)
			return nil
		}

		return fmt.Errorf("get replicated generation: %w", err)
	}
```

**File:** internal/praefect/replicator.go (L152-163)
```go
	}

	if generation != datastore.GenerationUnknown {
		return dr.rs.SetGeneration(ctx,
			event.Job.RepositoryID,
			event.Job.TargetNodeStorage,
			event.Job.RelativePath,
			generation,
		)
	}

	return nil
```

**File:** internal/praefect/datastore/repository_store_test.go (L436-448)
```go
	t.Run("GetReplicatedGeneration", func(t *testing.T) {
		t.Run("no previous record allowed", func(t *testing.T) {
			rs := newRepositoryStore(t, nil)

			gen, err := rs.GetReplicatedGeneration(ctx, 1, "source", "target")
			require.NoError(t, err)
			require.Equal(t, GenerationUnknown, gen)

			require.NoError(t, rs.CreateRepository(ctx, 1, vs, repo, "replica-path", "source", nil, nil, false, false))
			gen, err = rs.GetReplicatedGeneration(ctx, 1, "source", "target")
			require.NoError(t, err)
			require.Equal(t, 0, gen)
		})
```
