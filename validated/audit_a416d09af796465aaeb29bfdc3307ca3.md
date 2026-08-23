### Title
Unhandled error from batched reference updates in optimistic `ResetRefs()` silently reports success while refs remain unrestored - ([File: internal/backup/repository.go])

### Summary
`ResetRefs()` in the backup/restore path is Gitaly's closest analog to the ERC20 `timeLockERC20()` bug class: it invokes a critical, state-changing batched operation and, in the "optimistic" mode, discards the returned error entirely with `_ = rr.sendRefUpdates(...)` before unconditionally returning `nil`. Callers (and downstream restore logic/consumers) therefore have no way of knowing that the ref-reset operation actually failed, and will proceed as though the repository's reference state matches the intended backup state when it may not.

### Finding Description
`ResetRefs` builds a batch of delete and update operations from a diff between the current ref state and the desired backup ref state, then applies them via `sendRefUpdates`, a wrapper around the `UpdateReferences` streaming RPC: [1](#0-0) 

In the `optimistic` branch, deletions are required to succeed (their error is checked and propagated), but the subsequent batch of *updates* has its error explicitly discarded: [2](#0-1) 

```go
if err := rr.sendRefUpdates(ctx, refClient, removeUpdates); err != nil {
    return fmt.Errorf("remove refs: %w", err)
}
_ = rr.sendRefUpdates(ctx, refClient, updates)

return nil
```

This mirrors the ERC20 finding precisely: rather than treating the return value of a state-mutating operation (`transferFrom` there, `sendRefUpdates`/`UpdateReferences` here) as authoritative, the code assumes success and lets the caller believe the operation completed as requested. The accompanying comment acknowledges this is deliberate ("Regular update operations are allowed to fail to maintain backward compatibility"), but the implementation goes further than "allowed to fail gracefully" — it hides *all* information about the failure, including partial failures where some but not all ref updates in the batch succeeded (`UpdateReferences` applies updates as an atomic transaction per stream but the code doesn't distinguish which updates landed and doesn't log or surface the error at all).

### Impact Explanation
Because the caller of `ResetRefs` (e.g., repository restore workflows) receives `nil` even when reference updates fail, the on-disk state of a restored repository's references can silently diverge from the recorded backup content. Anything that trusted the restore having succeeded — subsequent hooks, consistency checks, or reliance on the backup's declared ref-to-object mapping — can then encounter dangling/missing refs, or refs still pointing at pre-restore (now potentially garbage-collected/pruned) objects, without any error signal indicating the restore was incomplete. This is analogous to Bob in the ERC20 report discovering only *after the fact* that funds he was told were vaulted for him were never actually transferred.

### Likelihood Explanation
The `updates` batch commonly fails on legitimate operational conditions — e.g. a ref pointing to an object that doesn't yet exist in the target repository at that point in the restore sequence, concurrent modification, or reftable transaction conflicts (as called out directly in the surrounding comment). Since `RestoreRepository`/backup-restore flows are part of the routine (non-privileged-node) Gitaly RPC surface reachable through crafted RPC input, and the failure mode requires no attacker action beyond a normal restore under any of these common conditions, the likelihood of silently divergent ref state is realistic, not merely theoretical.

### Recommendation
Do not discard the error from the second `sendRefUpdates` call. At minimum:
- Log the error with enough context (which refs failed) so operators can detect and reconcile silent partial failures.
- Consider returning an aggregate/soft error (e.g., via `errors.Join` or a dedicated "partial failure" sentinel) instead of `nil`, so that callers who need strict consistency guarantees can distinguish a fully successful restore from a partially-applied one.
- If backward compatibility genuinely requires "best effort" semantics, that decision should be made by the caller after being informed of the failure, not hidden inside `ResetRefs`.

### Proof of Concept
1. Set up a repository backup where the desired ref state includes references pointing to commits not (yet) reachable in the target repository at the time updates are applied (e.g., due to incremental-backup gaps as documented in the surrounding code comments).
2. Invoke restore with `optimistic=true`, causing `ResetRefs` to call `sendRefUpdates(ctx, refClient, updates)` for the update batch.
3. Have `UpdateReferences` fail for one or more of the updates (e.g., missing target object).
4. Observe that `ResetRefs` still returns `nil`; the restore workflow logs no error and reports success.
5. Inspect the repository afterward: the affected refs still point at their pre-restore targets (not the intended backup targets), silently diverging from the backup's recorded ref list — confirmed by the existing test `TestRemoteRepository_ResetRefs`/`"failure with optimistic doesn't return error"` case, which explicitly asserts that failed updates leave stale ref targets while the call still reports success. [3](#0-2)

### Citations

**File:** internal/backup/repository.go (L328-415)
```go
func (rr *remoteRepository) ResetRefs(ctx context.Context, refs []git.Reference, optimistic bool) error {
	if len(refs) == 0 {
		return errors.New("empty refs list")
	}

	existingRefs := []git.Reference{}
	iterator, err := rr.ListRefs(ctx)
	if err != nil {
		return fmt.Errorf("list refs: %w", err)
	}
	for iterator.Next() {
		ref := iterator.Ref()
		existingRefs = append(existingRefs, ref)
	}
	if err := iterator.Err(); err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("list refs: %w", err)
	}

	objectHash, err := rr.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("object hash: %w", err)
	}

	refClient := rr.newRefClient()

	// Add updates to delete existing refs not in the new set
	refsToKeep := make(map[git.ReferenceName]struct{}, len(refs))
	for _, ref := range refs {
		refsToKeep[ref.Name] = struct{}{}
	}
	removeUpdates := make([]*gitalypb.UpdateReferencesRequest_Update, 0, len(existingRefs))
	for _, existingRef := range existingRefs {
		if shouldRemoveRef(refsToKeep, existingRef.Name) {
			removeUpdates = append(removeUpdates, &gitalypb.UpdateReferencesRequest_Update{
				Reference:   []byte(existingRef.Name),
				NewObjectId: []byte(objectHash.ZeroOID),
			})
		}
	}

	// Add updates to create or modify refs in the new set
	existingRefTargets := make(map[git.ReferenceName]string, len(existingRefs))
	for _, ref := range existingRefs {
		existingRefTargets[ref.Name] = ref.Target
	}
	updates := make([]*gitalypb.UpdateReferencesRequest_Update, 0, len(refs))
	for _, newRef := range refs {
		if shouldUpdateRef(existingRefTargets, newRef) {
			updates = append(updates, &gitalypb.UpdateReferencesRequest_Update{
				Reference:   []byte(newRef.Name),
				NewObjectId: []byte(newRef.Target),
			})
		}
	}

	if optimistic {
		// Separate delete and update operations to handle edge cases in incremental backups:
		// 1. Lightweight tags may exist in .refs but not in bundles (they reference existing objects)
		// 2. .refs and .bundle creation is not atomic - refs file is created first, so an object
		//    might be deleted between .refs creation and bundle generation
		// 3. By deleting refs first, we ensure repository state matches the backup even if some
		//    refs can't be updated due to missing objects in the bundle
		//
		// This approach replaces recreateRepo() while avoiding reftable transaction conflicts.
		// Regular update operations are allowed to fail to maintain backward compatibility -
		// previously we only fetched bundles without updating refs afterward, so failed updates
		// don't create a regression. Delete operations must succeed to ensure refs not in the
		// backup are removed (matching recreateRepo's behavior).
		//
		// TODO: Once our minimum Git version supports the --batch-update flag for git-update-ref,
		// we can combine these operations and let Git handle partial failures appropriately.
		if err := rr.sendRefUpdates(ctx, refClient, removeUpdates); err != nil {
			return fmt.Errorf("remove refs: %w", err)
		}
		_ = rr.sendRefUpdates(ctx, refClient, updates)

		return nil
	}

	allUpdates := make([]*gitalypb.UpdateReferencesRequest_Update, 0, len(removeUpdates)+len(updates))
	allUpdates = append(allUpdates, removeUpdates...)
	allUpdates = append(allUpdates, updates...)
	if err := rr.sendRefUpdates(ctx, refClient, allUpdates); err != nil {
		return fmt.Errorf("update refs: %w", err)
	}

	return nil
}
```

**File:** internal/backup/repository_test.go (L121-144)
```go
			desc: "failure with optimistic doesn't return error",
			setup: func(tb testing.TB, repo backup.Repository, updates map[string]string) ([]git.Reference, []git.Reference) {
				// "Snapshot" the refs to pretend this is our backup.
				backupRefState, err := getRefs(ctx, repo)
				require.NoError(t, err)
				backupRefState = removeHeadReference(backupRefState)

				expectedRefState := make([]git.Reference, len(backupRefState))
				for i, ref := range backupRefState {
					expectedRefState[i] = ref
					// Setting the target to the updated value, which means reset refs failed to update them.
					// Therefore, they still point to the updated ones before the ResetRef was called.
					if val, ok := updates[ref.Name.String()]; ok {
						expectedRefState[i].Target = val
					}

					// Set references to an invalid ObjectID to trigger error
					backupRefState[i].Target = "invalid-object-id"
				}

				return backupRefState, expectedRefState
			},
			optimistic: true,
		},
```
