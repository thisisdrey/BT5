Confirmed: `Commit()` is invoked exactly once per RPC by the interceptors (`internal/grpc/middleware/mvcc/mvcc.go:75` and `:118`), and any error is returned directly to the client without an internal retry against the same `LocalCache` instance. This matters for the analog below.

### Title
Premature `committed` flag set before the manifest CAS or artifact upload completes causes silent, unretriable loss of mutator-RPC writes - (File: `internal/git/mvcc/cache.go`)

### Summary
`LocalCache.Commit` flips its at-most-once guard `m.committed = true` before performing any of the actual publishing work (reading the new manifest hash, uploading artifacts, or doing the compare-and-swap on the canonical manifest pointer). This mirrors the H-13 pattern: a "done" flag is set before the operation it's supposed to gate has actually happened, so any legitimate later attempt to complete the same logical operation is permanently rejected by the guard even though nothing was actually committed.

### Finding Description
`Commit()` sets `m.committed = true` at [1](#0-0)  before it has read `m.ManifestPath()`, uploaded the new artifacts via `putNewArtifacts`, or executed the durable compare-and-swap `m.rs.Commit(ctx, m.repositoryID, m.baseHash, newHash)` at [2](#0-1) . If any of these subsequent steps fails (artifact upload I/O error, `m.rs.Commit` returning `ErrManifestPointerConflict` or any other transient storage error), `Commit()` returns an error, but `m.committed` is now permanently `true` on that `LocalCache` instance. Any further call to `Commit()` on the same instance short-circuits immediately with `"mvcc: commit called more than once"` at [3](#0-2) , never attempting the read/upload/CAS again — even though the real publish never happened.

The `Cache` interface and design docs describe a planned conflict-resolution path where a `Commit` failure triggers a three-way merge and a re-`Commit` attempt against the same session (`doc/mvcc_rpc_flow.md:294-299`, `:498-511`): "If the CAS fails, then a three-way merge is attempted... Commit returns error → three way merge → Commit(ctx, keym, baseHash, newHash)" [4](#0-3) . Because the `committed` flag is latched to `true` on the very first (failed) attempt, that retry-after-merge flow — the documented intended behavior — is unreachable in the current `LocalCache` implementation: a second `Commit()` call on the same `LocalCache` after a conflict can never actually run the CAS, it just returns the generic "commit called more than once" error, masking the real conflict/error and preventing recovery.

### Impact Explanation
This is directly analogous to the referenced bug class: a state flag is advanced to signal "operation done" ahead of the operation's actual completion, defeating the very state machine meant to gate execution. Here the practical effect is a reference-backend write-availability bug reachable from an ordinary user's mutator RPC (any Git push/reference update against an MVCC-backend repository routes through `UnaryInterceptor`/`StreamInterceptor` in `internal/grpc/middleware/mvcc/mvcc.go:47-124`). On any transient failure of the artifact upload or CAS step, the RPC's `LocalCache` becomes permanently unable to publish that RPC's changes, even if the caller (or a future retry-with-merge mechanism operating on the same cache/session) attempts to complete the commit — the guard blocks the retry rather than the actual conflicting write. This can silently strand a user's committed Git objects/artifacts (already uploaded, per the comment at `cache.go:227-228` "artifacts uploaded before advancing the pointer") without ever advancing the canonical manifest pointer, and forecloses the documented three-way-merge recovery path, turning a recoverable transient conflict into a hard, unrecoverable RPC failure.

### Likelihood Explanation
Reachable by any user performing a normal write RPC against a repository using the MVCC reference backend — no privileged access, malicious peer, or token leak required. It's triggered purely by ordinary contention (concurrent mutator RPCs racing to advance the manifest pointer, as exercised in `TestIntegrationConcurrentWriters`, `internal/git/mvcc/integration_test.go:182-254`) or transient artifact-upload failures, both of which are expected, not exceptional, occurrences in a production system with concurrent pushes.

### Recommendation
Only set `m.committed = true` after the manifest read, artifact upload, and canonical CAS have all succeeded (or move the guard to gate re-entrancy without conflating "attempted" with "succeeded"), e.g. set it just before returning `nil`, or use a separate `attempted`/`succeeded` pair of flags so a caller-driven retry (e.g., after a three-way merge, per the documented design) can still invoke the real publish logic instead of being rejected by the guard.

### Proof of Concept
1. Start a mutator RPC against an MVCC-backend repository; `CacheInterceptor` builds a `LocalCache` and calls `cache.Prepare`/`WaitUntilReady`, then after the handler succeeds, calls `cache.Commit(ctx)` (`internal/grpc/middleware/mvcc/mvcc.go:75` or `:118`).
2. Inside `Commit`, `m.committed` is set `true` at `cache.go:216`.
3. Force `m.rs.Commit` to fail with `ErrManifestPointerConflict` (e.g. by having a concurrent writer win the CAS, as in `TestIntegrationConcurrentWriters`) — `Commit()` returns the conflict error, `mapCommitError` maps it to `Aborted` in the interceptor.
4. If any code path (present or future, per the documented three-way-merge retry design) calls `cache.Commit(ctx)` again on the same `LocalCache` to retry after resolving the conflict, it immediately gets `"mvcc: commit called more than once"` from `cache.go:214` instead of performing the retried CAS — the retry is a no-op that can never publish the change.

### Citations

**File:** internal/git/mvcc/cache.go (L211-216)
```go
	// Assert the at-most-once property: advancing the canonical pointer more than
	// once for a single RPC would result in a torn write.
	if m.committed {
		return fmt.Errorf("mvcc: commit called more than once")
	}
	m.committed = true
```

**File:** internal/git/mvcc/cache.go (L227-234)
```go
	// Upload the new artifacts before advancing the pointer so the pointer never
	// references an artifact that is not yet durable.
	if err := m.putNewArtifacts(ctx, newHash); err != nil {
		return fmt.Errorf("put new artifacts: %w", err)
	}

	// Atomically advance the canonical manifest pointer.
	return m.rs.Commit(ctx, m.repositoryID, m.baseHash, newHash)
```

**File:** doc/mvcc_rpc_flow.md (L294-299)
```markdown
Then, a first attempt is made to advance the manifest pointer by doing a "Compare And Swap (CAS)" with
the manifest that was uploaded previously. If it succeeds, then we return from the RPC handler.

If the CAS fails, then a three-way merge is attempted to try and resolve conflict.

If it still fails, an error is returned to the client.
```
