### Title
Silently ignored trie flush error in `writeBlockWithState` causes irrecoverable state loss - (File: core/blockchain.go)

### Summary
`BlockChain.writeBlockWithState` periodically flushes a matured trie to disk via `bc.triedb.Commit(header.Root, true)` and then immediately treats the operation as successful: it advances `bc.lastWrite`, resets `bc.gcproc`, and proceeds to `Dereference` the now "flushed" trie roots from the in‑memory triedb. The return value of `Commit` is discarded, so if the disk write fails, the code still wipes the corresponding trie nodes from memory as though they were durably persisted.

### Finding Description
In the non-archive garbage-collection branch of `writeBlockWithState`: [1](#0-0) 

```go
if bc.gcproc > flushInterval {
    header := bc.GetHeaderByNumber(chosen)
    if header == nil {
        log.Warn("Reorg in progress, trie commit postponed", "number", chosen)
    } else {
        ...
        // Flush an entire trie and restart the counters
        bc.triedb.Commit(header.Root, true)
        bc.lastWrite = chosen
        bc.gcproc = 0
    }
}
// Garbage collect anything below our required write retention
for !bc.triegc.Empty() {
    root, number := bc.triegc.Pop()
    if uint64(-number) > chosen {
        bc.triegc.Push(root, number)
        break
    }
    bc.triedb.Dereference(root)
}
```

`triedb.Database.Commit` (and its backend, e.g. `hashdb.Database.Commit`) returns an `error` on any batch-write failure: [2](#0-1) [3](#0-2) 

Every other call site of `triedb.Commit` in this file checks and propagates or logs the error:
- The archive-mode path returns the error directly: [4](#0-3) 
- The `Stop()` shutdown path explicitly checks the error before continuing: [5](#0-4) 

But the periodic GC-triggered commit at line 1731 does not check the error at all. `bc.lastWrite` and `bc.gcproc` are unconditionally updated as if the flush succeeded, and immediately afterward the garbage-collection loop calls `bc.triedb.Dereference(root)` for every trie root at or below `chosen` — removing them from the in-memory dirty set regardless of whether they were actually durably written.

This mirrors the reported Booster.sol pattern precisely: an operation that can fail (`withdrawAll()` / `triedb.Commit()`) is wrapped so its failure is swallowed, yet the surrounding code unconditionally marks the operation "done" (`pool.shutdown = true` / `bc.lastWrite = chosen`) and subsequently performs an irreversible step assuming success (blocking withdraw retries / dereferencing trie nodes from memory) with no mechanism to detect or repair the resulting gap.

### Impact Explanation
If the underlying disk write fails (I/O error, disk full, corrupted batch, etc.), the trie nodes for the flushed root and everything at/below `chosen` are removed from memory via `Dereference` without having reached disk. Because the hashdb backend keeps referenced/dirty nodes only in memory until `Commit`, and only the flush-list bookkeeping (not durability) gates the uncaching in `cleaner.Put`, an unnoticed write failure combined with the unconditional dereference below leaves that historical state root neither in memory nor durably on disk. This is a "persisted head or state that differs from what was executed" — future access to that state (state sync serving, `debug_` APIs, re-orgs requiring that state, archive-style historical queries) will find the state root unavailable/corrupted, and there is no automatic recovery path since the flush was recorded as complete (`bc.lastWrite` advanced, `bc.gcproc` reset).

### Likelihood Explanation
This code executes on every full (non-archive) node under normal operating conditions whenever the GC flush interval is exceeded — it is a routine, frequently-hit path, not a rare edge case. The trigger is any transient disk-write failure during that specific `Commit` call (ENOSPC, I/O error, OS-level write failure), which is a realistic, if not attacker-controlled, failure mode; it is directly analogous to the acknowledged-valid Medium-severity finding in the report, which was accepted despite the reporter not enumerating a concrete failure trigger for `withdrawAll()`.

### Recommendation
Check and act on the error returned by `bc.triedb.Commit(header.Root, true)` at line 1731, consistent with how it is already handled in the archive-mode branch and in `Stop()`. On failure, do not advance `bc.lastWrite`/reset `bc.gcproc`, and do not proceed to dereference the corresponding trie roots from memory — retry the commit or escalate (e.g., `log.Crit`) so the node halts rather than silently losing durable state, mirroring the recommended fix of only marking an operation complete when the underlying action actually succeeded.

### Proof of Concept
1. Run a full (non-archive) node with `writeBlockWithState` reaching the `bc.gcproc > flushInterval` branch during normal block processing.
2. Inject a transient failure in the underlying batch write used by `hashdb.Database.Commit` (e.g., simulate a disk-full/I/O error on `batch.Write()`), so `Commit` returns a non-nil error.
3. Observe that `writeBlockWithState` ignores this error, sets `bc.lastWrite = chosen` and `bc.gcproc = 0`, and the following garbage-collection loop calls `bc.triedb.Dereference(root)` for all `triegc` entries with `number <= chosen`.
4. The corresponding trie state is now absent from both the in-memory dirty cache and disk. Subsequent reads of that historical state root (e.g., via state trie access for that block) fail/are unavailable, demonstrating a persisted-state divergence from what was actually executed, with no automatic repair mechanism.

### Citations

**File:** core/blockchain.go (L1380-1394)
```go
			for _, offset := range []uint64{0, 1, state.TriesInMemory - 1} {
				if number := bc.CurrentBlock().Number.Uint64(); number > offset {
					recent := bc.GetBlockByNumber(number - offset)

					log.Info("Writing cached state to disk", "block", recent.Number(), "hash", recent.Hash(), "root", recent.Root())
					if err := triedb.Commit(recent.Root(), true); err != nil {
						log.Error("Failed to commit recent state trie", "err", err)
					}
				}
			}
			if snapBase != (common.Hash{}) {
				log.Info("Writing snapshot state to disk", "root", snapBase)
				if err := triedb.Commit(snapBase, true); err != nil {
					log.Error("Failed to commit recent state trie", "err", err)
				}
```

**File:** core/blockchain.go (L1693-1696)
```go
	// If we're running an archive node, always flush
	if bc.cfg.ArchiveMode {
		return bc.triedb.Commit(root, false)
	}
```

**File:** core/blockchain.go (L1717-1734)
```go
	// If we exceeded time allowance, flush an entire trie to disk
	if bc.gcproc > flushInterval {
		// If the header is missing (canonical chain behind), we're reorging a low
		// diff sidechain. Suspend committing until this operation is completed.
		header := bc.GetHeaderByNumber(chosen)
		if header == nil {
			log.Warn("Reorg in progress, trie commit postponed", "number", chosen)
		} else {
			// If we're exceeding limits but haven't reached a large enough memory gap,
			// warn the user that the system is becoming unstable.
			if chosen < bc.lastWrite+state.TriesInMemory && bc.gcproc >= 2*flushInterval {
				log.Info("State in memory for too long, committing", "time", bc.gcproc, "allowance", flushInterval, "optimum", float64(chosen-bc.lastWrite)/state.TriesInMemory)
			}
			// Flush an entire trie and restart the counters
			bc.triedb.Commit(header.Root, true)
			bc.lastWrite = chosen
			bc.gcproc = 0
		}
```

**File:** triedb/database.go (L174-182)
```go
// Commit iterates over all the children of a particular node, writes them out
// to disk. As a side effect, all pre-images accumulated up to this point are
// also written.
func (db *Database) Commit(root common.Hash, report bool) error {
	if db.preimages != nil {
		db.preimages.commit(true)
	}
	return db.backend.Commit(root, report)
}
```

**File:** triedb/hashdb/database.go (L416-423)
```go
		log.Error("Failed to commit trie from trie database", "err", err)
		return err
	}
	// Trie mostly committed to disk, flush any batch leftovers
	if err := batch.Write(); err != nil {
		log.Error("Failed to write trie to disk", "err", err)
		return err
	}
```
