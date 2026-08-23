### Title
Crafted quarantine fields on Accessor RPCs bypass Gitaly's transaction/snapshot management - ([File: internal/gitaly/storage/storagemgr/middleware.go])

### Summary
The zkSync report shows that a check meant to be enforced on a single trusted path (`_validateTransaction`'s sequential-nonce check) can be skipped entirely by reaching the same state-mutating function (`incrementMinNonceIfEquals`/`updateNonceOrdering`) through an alternate, less-restricted entry point (an L1 priority transaction), permanently corrupting account invariants. The Gitaly analog is `beginTransactionForRepository` in `internal/gitaly/storage/storagemgr/middleware.go`, where the presence of `GitObjectDirectory`/`GitAlternateObjectDirectories` on the request is used as the sole signal that the request is a legitimate "loop back from Rails access-checks" call, and is allowed to skip the transaction manager's snapshot machinery for Accessor RPCs.

### Finding Description
`beginTransactionForRepository` decides whether an RPC runs inside Gitaly's transaction/snapshot manager or is routed non-transactionally: [1](#0-0) 

The comment on this code explicitly documents the underlying assumption and its weakness: the object directories "should only be configured on a repository coming from a request that was already configured with a quarantine directory and is being looped back to Gitaly from Rails' authorization checks," but this is enforced only by the *presence of the field*, not by any cryptographic or session-bound proof that the value actually originated from a Gitaly-issued quarantine. For `OpMutator` RPCs this is explicitly rejected (`ErrQuarantineConfiguredOnMutator`), matching the code comment for `updateNonceOrdering`-style restrictions on the "main" path. However, for `OpAccessor` RPCs, any client that sets these two fields on the request is routed through `restoreSnapshotRelativePath` and executed as a `nonTransactionalRequest`, i.e., completely outside of the `TransactionManager`'s snapshot/MVCC isolation: [2](#0-1) 

This mirrors the zkSync bug class precisely: the intended invariant ("only Rails' looped-back accessor calls, which are already inside a transaction, may carry quarantine directories") is enforced only for the mutator path, while the accessor path is an alternate, unauthenticated route that any RPC caller can exercise by simply populating `GitObjectDirectory`/`GitAlternateObjectDirectories` in a plain `Repository` message — no Rails round-trip, no transaction ID, no proof of prior state is required.

### Impact Explanation
An ordinary RPC caller sending an Accessor RPC (e.g., `ObjectFormat`, `IsAncestor`, size/stat RPCs) with a crafted `GitObjectDirectory`/`GitAlternateObjectDirectories` value causes Gitaly to skip transaction begin/commit/snapshot resolution for that call entirely, and the request is executed against whatever relative path is provided via `restoreSnapshotRelativePath`/the `MetadataKeySnapshotRelativePath` header instead of the transactionally-resolved snapshot. This breaks the "every repository-scoped RPC runs against the manager's authoritative snapshot" invariant, which is foundational for Gitaly Cluster's read consistency and for correct object quarantine isolation. Combined with directory validation performed later in `ObjectDirectoryPath`/`InfoAlternatesPath` (which restrict the object directory to the repo path or an expected quarantine-prefixed temp path but do not equally restrict `GitAlternateObjectDirectories`), this creates a path where an unprivileged client can force the RPC to run non-transactionally, defeating the purpose of Gitaly's MVCC/quorum-based transaction system for that call. The project's own code comment flags this as a known, unaddressed gap ("We'll leave this unaddressed for now").

### Likelihood Explanation
Reachability requires nothing more than an ordinary gRPC client setting two optional fields already present on `gitalypb.Repository` (`git_object_directory`, `git_alternate_object_directories`), which are legitimate, documented fields used elsewhere for quarantine looping. No authentication bypass or malicious peer is needed beyond having any valid Gitaly client access (i.e., a standard push/fetch/API caller), matching the "ordinary user... crafted RPC field" reachability requirement.

### Recommendation
Do not use the mere presence of `GitObjectDirectory`/`GitAlternateObjectDirectories` as sufficient proof that a request is a legitimate Rails loop-back call. Bind quarantine looping to an authenticated/verifiable session token (e.g., validate against the `MetadataKeySnapshotRelativePath` plus a signed/opaque transaction reference minted by Gitaly when quarantine was first established, similar to how `QuarantineDirectoryPrefix` binds a quarantine path to a repo hash) so that Accessor RPCs cannot silently escape transactional snapshot handling using attacker-supplied field values alone. Extend the existing mutator-only rejection (`ErrQuarantineConfiguredOnMutator`) logic to validate accessor-path quarantine values against the actual quarantine state recorded by the `TransactionManager`, not just field presence.

### Proof of Concept
1. As an ordinary Gitaly client, issue any Accessor-scoped RPC (e.g., `RepositoryService.ObjectFormat`) against a target repository.
2. Populate the request's `Repository.GitObjectDirectory` and/or `GitAlternateObjectDirectories` fields with attacker-chosen values (optionally along with the `MetadataKeySnapshotRelativePath` gRPC metadata key, which is not cryptographically tied to any real transaction).
3. Observe in `beginTransactionForRepository` (`internal/gitaly/storage/storagemgr/middleware.go:271-296`) that because the object directory fields are non-empty and the RPC is `OpAccessor`, the request bypasses `storageHandle.Begin(...)` entirely and is dispatched via `nonTransactionalRequest`, executing outside of the transaction manager's snapshot isolation — regardless of whether the caller ever actually participated in a real quarantine/transaction flow.

### Citations

**File:** internal/gitaly/storage/storagemgr/middleware.go (L271-297)
```go
	if targetRepo.GetGitObjectDirectory() != "" || len(targetRepo.GetGitAlternateObjectDirectories()) > 0 {
		// The object directories should only be configured on a repository coming from a request that
		// was already configured with a quarantine directory and is being looped back to Gitaly from Rails'
		// authorization checks. If that's the case, the request should already be running in scope of a
		// transaction and the repository rewritten to point to the snapshot repository. We thus don't start
		// a new transaction if we encounter this.
		//
		// This property is violated in tests which manually configure the object directory or the alternate
		// object directory. This allows for circumventing the transaction management by configuring the either
		// of the object directories. We'll leave this unaddressed for now and later address this by removing
		// the options to configure object directories and alternates in a request.

		if methodInfo.Operation == protoregistry.OpMutator {
			// Accessor requests may come with quarantine configured from Rails' access checks. Since the
			// RPC that triggered these access checks would already run in a transaction and target a
			// snapshot, we won't start another one. Mutators however are rejected to prevent writes
			// unintentionally targeting the main repository.
			return transactionalizedRequest{}, ErrQuarantineConfiguredOnMutator
		}

		rewrittenReq, err := restoreSnapshotRelativePath(ctx, methodInfo, req)
		if err != nil {
			return transactionalizedRequest{}, fmt.Errorf("restore snapshot relative path: %w", err)
		}

		return nonTransactionalRequest(ctx, rewrittenReq), nil
	}
```
