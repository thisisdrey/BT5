### Title
`RewriteHistory` bypasses housekeeping interval throttling, allowing unbounded forced git repacks/prunes — ([File: internal/grpc/middleware/housekeeping/middleware.go])

### Summary
The external report describes `Voter.poke()`, which is meant to simply re-cast an existing vote, but as a side effect always calls `accrueFlux`, letting a user repeatedly invoke it to accrue an unlimited amount of a normally rate-limited resource, bypassing the intended once-per-epoch cadence. The Gitaly analog is the housekeeping middleware's `forceHousekeepingRPCs` mechanism: a specific mutator RPC (`CleanupService.RewriteHistory`) is special-cased to always force a full `OptimizeRepository` run, completely bypassing the `RPCInterval` throttling that governs every other mutator RPC.

### Finding Description
`Middleware.UnaryServerInterceptor`/`StreamServerInterceptor` schedule housekeeping (`git-pack-refs`, `git-repack`, prune, commit-graph write) after mutator RPCs, but only once the per-repository `writeCount` exceeds an operation's configured `RPCInterval` [1](#0-0) . This is the intended backpressure: expensive git maintenance should run at most once every N writes.

However, `forceHousekeepingRPCs` marks `CleanupService_RewriteHistory` as always-force [2](#0-1) . When this RPC completes, `scheduleHousekeeping` is called with `force=true` [3](#0-2) , and `pendingOperations` returns every operation in `allOperations` unconditionally when `force` is set, ignoring `writesSinceLastRun` entirely [4](#0-3) . The only guard preventing back-to-back invocations is `a.active`, which is only true while a housekeeping goroutine from a *prior* call is still running [5](#0-4) . As soon as that goroutine finishes and calls `markHousekeepingInactive`, an ordinary user with write access to the repository can call `RewriteHistory` again, forcing another complete `OptimizeRepository` cycle (`repack -a -d`-class objects/refs repack, prune, commit-graph rewrite) — with no minimum-interval requirement, unlike every other mutator RPC. Test coverage explicitly documents this intended-but-unthrottled bypass behavior: "forceHousekeepingRPCs bypass the normal interval constraint" [6](#0-5) .

This mirrors the smart-contract bug precisely: a function (`poke`/`RewriteHistory`) that is supposed to be an ordinary, occasionally-useful operation triggers an expensive/rate-limited side effect (`accrueFlux`/`OptimizeRepository`) every single time it is called, without the interval-based gate that protects the same side effect when triggered through the normal path.

### Impact Explanation
An unprivileged user who can push to / call `CleanupService.RewriteHistory` on a repository can repeatedly (in a tight loop, back-to-back as soon as each call returns) force full `git-repack`, prune, and commit-graph rewrite cycles on that repository, entirely bypassing the `RPCInterval` design that exists specifically to bound how often these expensive operations run (the concurrency/backpressure system elsewhere in Gitaly, e.g. `doc/backpressure.md`, exists for exactly this class of resource-exhaustion risk [7](#0-6) ). This is a resource-exhaustion / DoS vector against the Gitaly node hosting the repository: repeated full repacks and prunes consume disproportionate CPU, disk I/O, and temporarily hold repository locks, degrading service for that repository (and potentially the node, given Gitaly's node-wide resource model) far beyond what the RPC-interval throttle was designed to permit.

### Likelihood Explanation
`RewriteHistory` is a standard `CleanupService` RPC reachable by any actor with ordinary write access to a repository (the same access level needed to push) — no special privilege, leaked token, or malicious peer is required. The bypass is triggered by simply calling the RPC in a loop; the only limiting factor is that a fresh `OptimizeRepository` run must finish before the next can be scheduled, which is a much weaker constraint than the `RPCInterval` (default 10–20 writes) applied to every other mutator.

### Recommendation
Apply the same `RPCInterval`/threshold gating to `forceHousekeepingRPCs` as to regular mutators, or introduce a minimum wall-clock cooldown between forced housekeeping runs per repository (independent of `a.active`), so that an actor cannot force unlimited repacks/prunes simply by repeatedly invoking `RewriteHistory`. Alternatively, subject `RewriteHistory`-triggered housekeeping to the same concurrency/queue limiter used elsewhere (`internal/limiter`) so repeated invocations are throttled at the RPC layer.

### Proof of Concept
1. As a user with push/write access to a repository, call `CleanupService.RewriteHistory` with a trivial redaction pattern.
2. Observe (per `middleware_test.go`) that housekeeping (`OptimizeRepository`) is invoked immediately, regardless of `writeCount` [8](#0-7) .
3. Repeat step 1 in a loop as soon as each call completes (polling `active` via subsequent calls succeeding). Each iteration forces a brand-new full `OptimizeRepository` cycle, with no `RPCInterval` wait required, unlike identical repacking triggered via normal mutator RPCs (`WriteRef`, etc.), which require multiple RPCs to accumulate before housekeeping runs [9](#0-8) .

### Citations

**File:** internal/grpc/middleware/housekeeping/middleware.go (L98-101)
```go
// forceHousekeepingRPCs are all the RPCs that we should force housekeeping right after.
var forceHousekeepingRPCs = map[string]struct{}{
	gitalypb.CleanupService_RewriteHistory_FullMethodName: {},
}
```

**File:** internal/grpc/middleware/housekeeping/middleware.go (L196-199)
```go
			}

			_, forceHousekeeping := forceHousekeepingRPCs[methodInfo.FullMethodName()]
			m.scheduleHousekeeping(ctx, targetRepo, forceHousekeeping)
```

**File:** internal/grpc/middleware/housekeeping/middleware.go (L306-321)
```go
// pendingOperations returns operations that have exceeded their RPC interval thresholds.
func (m *Middleware) pendingOperations(a *activity, force bool) []config.OperationType {
	var pending []config.OperationType

	for _, op := range allOperations {
		thresholds := m.getThresholds(op)
		lastRun := a.writeCountAtLastRun[op]
		writesSinceLastRun := a.writeCount - lastRun

		if force || writesSinceLastRun > thresholds.RPCInterval {
			pending = append(pending, op)
		}
	}

	return pending
}
```

**File:** internal/grpc/middleware/housekeeping/middleware.go (L337-357)
```go
func (m *Middleware) scheduleHousekeeping(ctx context.Context, repo *gitalypb.Repository, force bool) {
	m.mu.Lock()
	defer m.mu.Unlock()

	key := m.getRepoKey(repo)

	a, ok := m.repoActivity[key]
	if !ok {
		a = newActivity()
		m.repoActivity[key] = a
	}
	a.writeCount++

	if a.active {
		return
	}

	pendingOps := m.pendingOperations(a, force)
	if len(pendingOps) == 0 {
		return
	}
```

**File:** internal/grpc/middleware/housekeeping/middleware_test.go (L633-661)
```go
	t.Run("when forceHousekeepingRPCs bypass interval compared to regular mutators", func(t *testing.T) {
		forceRepo := &gitalypb.Repository{
			RelativePath: "myrepo-force-bypass",
		}
		regularRepo := &gitalypb.Repository{
			RelativePath: "myrepo-regular-interval",
		}

		// Test that forceHousekeepingRPCs bypass the normal interval constraint
		// First RewriteHistory call should immediately trigger housekeeping (force=true)
		stream, err := gitalypb.NewCleanupServiceClient(conn).RewriteHistory(ctx)
		require.NoError(t, err)

		err = stream.Send(&gitalypb.RewriteHistoryRequest{
			Repository: forceRepo,
			Redactions: [][]byte{[]byte("test-pattern")},
		})
		require.NoError(t, err)

		_, err = stream.CloseAndRecv()
		require.NoError(t, err)

		housekeepingMiddleware.WaitForWorkers()

		// Should trigger housekeeping immediately despite being the first call
		require.Equal(t, 1,
			housekeepingManager.getOptimizeRepositoryInvocations(forceRepo.GetRelativePath()),
			"First RewriteHistory call should force housekeeping immediately")

```

**File:** internal/grpc/middleware/housekeeping/middleware_test.go (L662-672)
```go
		// Compare with regular mutator RPCs that respect the interval
		// Single regular mutator call should not trigger housekeeping
		_, err = gitalypb.NewRepositoryServiceClient(conn).WriteRef(ctx, &gitalypb.WriteRefRequest{
			Repository: regularRepo,
		})
		require.NoError(t, err)

		housekeepingMiddleware.WaitForWorkers()

		require.Equal(t, 0, housekeepingManager.getOptimizeRepositoryInvocations(regularRepo.GetRelativePath()),
			"Single regular mutator should not trigger housekeeping (respects interval)")
```

**File:** doc/backpressure.md (L1-12)
```markdown
# Request limiting in Gitaly

In the GitLab ecosystem, Gitaly is the service that is at the bottom of the
stack for Git data access. This means that when there is a surge of
requests to retrieve or change a piece of Git data, the I/O happens in Gitaly.
This can lead to Gitaly being overwhelmed due to system resource exhaustion
because all Git access goes through Gitaly.

If there is a surge of traffic beyond what Gitaly can handle, Gitaly should
be able to push back on the client calling. Gitaly shouldn't subserviently agree
to process more than it can handle.

```
