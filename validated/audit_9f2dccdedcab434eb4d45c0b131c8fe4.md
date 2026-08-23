Based on the investigation, the strongest analog I found to the `DepositQueue.remove` gas-exhaustion bug class is a quadratic-cost loop in `TransactionManager.UpdateReferences`, triggered by an ordinary user's push through reference-transaction hook batching.

### Title
DoS via unbounded quadratic-cost reference batch scan in `Transaction.UpdateReferences` - (File: `internal/gitaly/storage/storagemgr/partition/transaction_manager.go`)

### Summary
Similar to the `DepositQueue.remove` bug — where every new (cheap) queue entry made every future removal operation more expensive, eventually exceeding the block gas limit — Gitaly's `Transaction.UpdateReferences` accumulates one entry per invocation into `txn.referenceUpdates`, and on every subsequent invocation re-scans **all previously accumulated batches** for each reference being updated. An ordinary push that forces many sequential reference-transaction hook invocations within a single Gitaly transaction can drive this scan to quadratic cost.

### Finding Description
`Transaction.UpdateReferences` is documented as being callable multiple times within the same transaction — "Each call is treated as a different reference transaction" [1](#0-0) , explicitly to support cases like deleting `refs/heads/parent` in one call and creating `refs/heads/parent/child` in a subsequent call within the same push.

For every call, the function loops over each reference in the current update and, for each one, does a nested scan over **every previously staged batch** in `txn.referenceUpdates` to resolve intermediate OID chains: [2](#0-1) 

The result is appended, growing the batch list further: [3](#0-2) 

This is invoked from `ReferenceTransactionHook` when Praefect/WAL transactions are enabled, once per `ReferenceTransactionCommitted` event fired by `git receive-pack`/`git-update-ref`: [4](#0-3) 

Git splits a push's ref updates into multiple sequential reference-transactions (each separately triggering the hook) when there are directory/file naming conflicts among the refs being updated in that push — e.g., deleting `refs/heads/a` while creating `refs/heads/a/b`, or a deep chain `refs/heads/a` → `refs/heads/a/b` → `refs/heads/a/b/c` → … An attacker crafting a single push with N such chained directory/file-conflicting ref transitions can force N sequential hook invocations against the same Gitaly transaction. Each invocation costs O(current batch count), so N such invocations sum to O(N²) — mirroring the `DepositQueue.remove` pattern where cheap, attacker-controlled growth of a list makes a linear per-op cost sum to quadratic total cost.

### Impact Explanation
If reachable in practice, this would allow an unprivileged pushing user to make a single push RPC (e.g. `PostReceivePack`) consume disproportionate CPU inside the `TransactionManager`'s single-threaded transaction-processing path, potentially exceeding request/RPC time budgets and denying service for that repository's transaction processing (a DoS of the push/reference-update handler), consistent with the "DoS of a handler" acceptance category.

### Likelihood Explanation
Likelihood is **uncertain** and requires further verification: I was not able to conclusively confirm, within the constraints of this read-only investigation, how many separate reference-transaction hook invocations Git will actually produce for a single push containing N chained directory/file-conflicting ref updates, nor whether Gitaly's own reference-conflict resolution (e.g., `refdb` tree in `internal/gitaly/storage/storagemgr/partition/conflict/refdb/tree.go`) already rejects or batches such changes before they reach `UpdateReferences` multiple times. Confirming actual quadratic blow-up requires dynamic testing (e.g., a benchmark push with many chained ref renames) that a background Devin agent with code execution could perform, whereas I only have static/read-only access here.

### Recommendation
- Determine the maximum practical number of reference-transaction hook invocations that a single push can trigger via directory/file-conflicting ref chains, and benchmark `UpdateReferences` cost under that condition.
- If a quadratic cost is confirmed, replace the linear per-call scan over `txn.referenceUpdates` with an O(1) or O(log n) lookup structure (e.g., a single cumulative `map[git.ReferenceName]git.ReferenceUpdate` updated incrementally instead of a slice of per-call maps scanned repeatedly), or cap the number of reference-transaction batches processed per Gitaly transaction.

### Proof of Concept
Not executed (no code-execution access in this session). A concrete PoC would involve constructing a single Git push containing a long chain of ref renames that create directory/file conflicts (e.g., `refs/heads/level0` → delete and create `refs/heads/level0/level1` → delete and create `refs/heads/level0/level1/level2` → … for N levels) and measuring `TransactionManager` commit latency for increasing N to confirm superlinear scaling of `UpdateReferences`.

### Citations

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L688-700)
```go
// UpdateReferences updates the given references as part of the transaction. Each call is treated as
// a different reference transaction. This allows for performing directory-file conflict inducing
// changes in a transaction. For example:
//
// - First call  - delete 'refs/heads/parent'
// - Second call - create 'refs/heads/parent/child'
//
// If a reference is updated multiple times during a transaction, its first recorded old OID used as
// the old OID when verifying the reference update, and the last recorded new OID is used as the new
// OID in the final commit. This means updates like 'oid-1 -> oid-2 -> oid-3' will ultimately be
// committed as 'oid-1 -> oid-3'. The old OIDs of the intermediate states are not verified when
// committing the write to the actual repository and are discarded from the final committed log
// entry.
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L701-749)
```go
func (txn *Transaction) UpdateReferences(ctx context.Context, updates git.ReferenceUpdates) error {
	u := git.ReferenceUpdates{}

	for reference, update := range updates {
		// Transactions should only stage references with valid names as otherwise Git would already
		// fail when they try to stage them against their snapshot. `update-ref` happily accepts references
		// outside of `refs` directory so such references could theoretically arrive here. We thus sanity
		// check that all references modified are within the refs directory.
		//
		// HEAD is a special case and refers to a default branch update.
		if !strings.HasPrefix(reference.String(), "refs/") && reference != "HEAD" {
			return InvalidReferenceFormatError{ReferenceName: reference}
		}

		oldOID := update.OldOID
		oldTarget := update.OldTarget

		if initialValue, ok := txn.initialReferenceValues[reference]; ok {
			if !initialValue.IsSymbolic {
				oldOID = git.ObjectID(initialValue.Target)
			} else {
				oldTarget = git.ReferenceName(initialValue.Target)
			}
		}

		if oldOID == update.NewOID && oldTarget == update.NewTarget {
			// This was a no-op.
			continue
		}

		for _, updates := range txn.referenceUpdates {
			if txUpdate, ok := updates[reference]; ok {
				if txUpdate.NewOID != "" {
					oldOID = txUpdate.NewOID
				}

				if txUpdate.NewTarget != "" {
					oldTarget = txUpdate.NewTarget
				}
			}
		}

		u[reference] = git.ReferenceUpdate{
			OldOID:    oldOID,
			NewOID:    update.NewOID,
			OldTarget: oldTarget,
			NewTarget: update.NewTarget,
		}
	}
```

**File:** internal/gitaly/storage/storagemgr/partition/transaction_manager.go (L757-767)
```go
	// ReferenceRecorder is not used with reftables.
	if txn.referenceRecorder != nil {
		if err := txn.referenceRecorder.RecordReferenceUpdates(ctx, updates); err != nil {
			return fmt.Errorf("record reference updates: %w", err)
		}
	}

	txn.referenceUpdates = append(txn.referenceUpdates, u)

	return nil
}
```

**File:** internal/gitaly/hook/referencetransaction.go (L87-103)
```go
	// We're also voting in committed state to tell Praefect we've actually persisted the
	// changes. This is necessary as some RPCs fail return errors in the response body rather
	// than as an error code. Praefect can't tell if these RPCs have failed. Voting on committed
	// ensure Praefect sees either a missing vote or that the RPC did commit the changes.
	case ReferenceTransactionCommitted:
		phase = voting.Committed

		if tx != nil {
			updates, err := parseChanges(objectHash, bytes.NewReader(changes))
			if err != nil {
				return fmt.Errorf("parse changes: %w", err)
			}

			if err := tx.UpdateReferences(ctx, updates); err != nil {
				return fmt.Errorf("update references: %w", err)
			}
		}
```
