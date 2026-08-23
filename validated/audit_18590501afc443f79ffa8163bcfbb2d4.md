### Title
Load shedder cancels arbitrary in-flight RPCs across all repositories/tenants based on node-wide resource pressure, enabling a single user to DoS unrelated users' Git operations - (File: internal/burdenmonitor/load_shedder.go)

### Summary
The `LoadShedder` cancels the top-N highest-resource in-flight RPCs whenever a *node-wide* (parent-cgroup) resource-pressure condition fires, without any scoping to the repository, user, or request that actually caused the pressure. Because the triggering signal is a shared, externally-influenceable aggregate (CPU/memory/IO PSI, OOM events measured on the parent cgroup), any ordinary client can drive that aggregate into "critical" territory through legitimate-looking but resource-heavy RPCs (e.g. large clone/fetch/push, or `CreateRepositoryFromBundle`), and the shedder then force-cancels the top 10 CPU-heaviest RPCs system-wide — which are very likely to be long-running, legitimate operations belonging to completely unrelated users/repositories.

### Finding Description
`LoadShedder.run` reacts to `loadmonitor.Event`s coming from PSI-critical and OOM conditions [1](#0-0) . These conditions are computed from the cgroup manager's `ParentStats`, i.e. the aggregate resource usage of the single parent cgroup shared by the whole Gitaly node/storage, not per-repository or per-request stats [2](#0-1) [3](#0-2) .

When a critical event fires, `shed()` unconditionally asks the `BurdenMonitor` for the globally top-N CPU-consuming in-flight RPCs across *all* tracked repositories and cancels every one of them, with no check on whether the cancelled RPC has anything to do with the RPC/repository that generated the pressure: [4](#0-3) .

`BurdenMonitor.GetTopNEntries`/`entriesSortedBy` simply ranks every registered `RPCEntry` in the whole process by cumulative CPU time, with no repository or tenant partitioning: [5](#0-4) . `RPCEntry.Cancel` (a `context.CancelCauseFunc`) is then invoked directly to abort the victim RPC with a `RESOURCE_EXHAUSTED` error: [6](#0-5) .

This mirrors the root-cause pattern of the referenced report: a globally shared, externally-manipulable state value (`availableAssets`/system pressure) feeds a gating decision (`neededAssetsForWithdraw`/critical-event shedding) whose consequence (withdrawal DoS/RPC cancellation) is imposed on an unrelated party rather than the actor who influenced the state.

### Impact Explanation
An unprivileged client can push, clone, fetch, or otherwise trigger CPU/memory/IO-heavy operations (e.g. repeated large clones, oversized packfile pushes, `CreateRepositoryFromBundle` imports) purely through normal RPC parameters, driving the node's aggregate PSI/CPU-throttle/OOM signals past the configured critical thresholds. Once tripped, the load shedder indiscriminately cancels the ten highest-CPU in-flight RPCs system-wide — these are commonly other users' large, long-running, entirely legitimate clone/fetch/push operations on unrelated repositories, since long-running commands accumulate more CPU time than the short bursts of an attacker's own trigger requests. This is a denial-of-service against arbitrary tenants sharing the node, without requiring any privilege, credential, or targeting of the specific victim repository.

### Likelihood Explanation
Likelihood is moderate: it requires the node to already be near a resource-pressure threshold (which large multi-tenant Gitaly nodes commonly are under legitimate load), and it requires the `featureflag.BurdenMonitorTrackCommands` gating to be enabled for the relevant RPCs. Any client capable of issuing normal Git RPCs (clone/fetch/push) can contribute to and exploit this shared signal; no crafted/malformed input is needed, only volume/size of otherwise-valid requests.

### Recommendation
Scope load-shedding decisions to the actor/repository responsible for the resource pressure rather than shedding the global top-N by raw CPU time: e.g., only shed RPCs belonging to the repository(ies)/cgroup whose resource usage triggered the condition, or weight shedding decisions by per-repository/per-cgroup attribution rather than node-wide aggregates. Alternatively, incorporate fairness/quota accounting (e.g., per-tenant CPU budget) so that an actor cannot cause cancellation of RPCs outside their own workload.

### Proof of Concept
1. An unprivileged client repeatedly issues resource-intensive but valid RPCs (e.g., concurrent large `PostUploadPack`/clone requests, or bundle imports) against the shared Gitaly node.
2. This drives the parent cgroup's PSI/CPU-throttle metrics past the critical thresholds evaluated in `newPSICriticalCondition`/`newOOMKillCondition`, emitting a `loadmonitor.Event`.
3. `LoadShedder.shed` (`internal/burdenmonitor/load_shedder.go:114-127`) queries `BurdenMonitor.GetTopNEntries(10, SortByCPU)` (`internal/burdenmonitor/burdenmonitor.go:129-161`), which ranks *all* in-flight RPCs on the node regardless of repository/tenant.
4. The 10 highest cumulative-CPU RPCs — likely other tenants' unrelated, legitimate large clone/fetch/push operations — are cancelled via `entry.Cancel(...)` with `RESOURCE_EXHAUSTED`, denying service to users who had no part in causing the resource pressure.

### Citations

**File:** internal/burdenmonitor/load_shedder.go (L69-86)
```go
	conditions := []loadmonitor.Condition{
		newPSICriticalCondition(psiResourceCPU, cfg.PSI.CPU),
		newPSICriticalCondition(psiResourceMemory, cfg.PSI.Memory),
		newPSICriticalCondition(psiResourceIO, cfg.PSI.IO),
		newOOMKillCondition(),
	}

	events, err := lm.NotifyOn(conditions...)
	if err != nil {
		return nil, fmt.Errorf("load shedder: subscribing to load monitor: %w", err)
	}

	return &LoadShedder{
		logger: logger,
		bm:     bm,
		events: events,
	}, nil
}
```

**File:** internal/burdenmonitor/load_shedder.go (L114-127)
```go
func (ls *LoadShedder) shed(ctx context.Context, event loadmonitor.Event) {
	entries := ls.bm.GetTopNEntries(shedTopN, SortByCPU)
	reason := SortByCPU.reason()

	for _, entry := range entries {
		ls.cancel(entry, reason)
	}

	ls.logger.WithFields(log.Fields{
		"condition":    event.ConditionName,
		"reason":       event.Description,
		"sniped_count": len(entries),
	}).WarnContext(ctx, "load shedder cancelled in-flight RPCs")
}
```

**File:** internal/burdenmonitor/load_shedder.go (L129-147)
```go
func (ls *LoadShedder) cancel(entry *RPCEntry, reason string) {
	err := structerr.NewResourceExhausted(
		"RPC cancelled by load shedder: %s", reason).
		WithDetail(&gitalypb.LimitError{ErrorMessage: reason})

	entry.Cancel(err)

	rpcsShedTotal.WithLabelValues(entry.ServiceName, entry.MethodName, reason).Inc()

	ls.logger.WithFields(log.Fields{
		"rpc_id":         entry.ID,
		"correlation_id": entry.CorrelationID,
		"repository":     entry.Repository,
		"reason":         reason,
		"cpu_time_ms":    entry.TotalCPUTime().Milliseconds(),
		"memory_bytes":   entry.TotalMemory(),
		"active_cmds":    entry.ActiveCommandCount(),
	}).WarnContext(entry.Context, "load shedder cancelled RPC")
}
```

**File:** internal/cgroups/manager_linux.go (L238-250)
```go
// Stats returns cgroup accounting statistics collected by reading
// cgroupfs files.
func (cgm *CGroupManager) Stats() (Stats, error) {
	parentStats, err := cgm.handler.stats(cgm.currentProcessCgroup())
	if err != nil {
		return Stats{}, err
	}

	return Stats{
		ParentStats: parentStats,
		version:     cgm.cgroupVersion,
	}, nil
}
```

**File:** internal/limiter/condition_cpu.go (L16-43)
```go
// newCPUThrottlingCondition returns a Condition that evaluates to true when CPU throttling occurred for more than 50% of the
// time between the last 2 polls.
func newCPUThrottlingCondition(threshold float64) loadmonitor.Condition {
	if threshold <= 0.0 {
		threshold = defaultCPUThrottlingThreshold
	}

	return loadmonitor.Condition{
		Name: conditionCgroupCPU,
		Fn: func(ctx context.Context, previous, current loadmonitor.Stats, pollInterval time.Duration) (bool, string) {
			cur, prev := current.CGroup.ParentStats, previous.CGroup.ParentStats

			// Somehow, cgroup stats are reset. It's usually the consequence of cgroup limits being changed.
			// Alternatively, they can be overridden by another program.
			// Either way, the watcher should update the stats accordingly.
			if cur.CPUThrottledCount < prev.CPUThrottledCount || cur.CPUThrottledDuration < prev.CPUThrottledDuration {
				return false, ""
			}

			throttledDuration := cur.CPUThrottledDuration - prev.CPUThrottledDuration

			// If the total throttled duration since the last poll exceeds 50%.
			if pollInterval > 0 && throttledDuration/pollInterval.Seconds() > threshold {
				return true, eventCPUThrottling
			}
			return false, ""
		},
	}
```

**File:** internal/burdenmonitor/burdenmonitor.go (L129-161)
```go
// EntriesSortedBy returns all tracked RPC entries sorted by the specified field.
// The returned entries are sorted in descending order (highest resource usage first).
func (bm *BurdenMonitor) entriesSortedBy(sortBy SortBy) []*RPCEntry {
	entries := bm.Entries()

	switch sortBy {
	case SortByCPU:
		sort.Slice(entries, func(i, j int) bool {
			return entries[i].TotalCPUTime() > entries[j].TotalCPUTime()
		})
	case SortByMemory:
		sort.Slice(entries, func(i, j int) bool {
			return entries[i].TotalMemory() > entries[j].TotalMemory()
		})
	case SortByDuration:
		sort.Slice(entries, func(i, j int) bool {
			return entries[i].StartTime.Before(entries[j].StartTime)
		})
	}

	return entries
}

// GetTopNEntries returns the top N tracked RPC entries sorted by the specified
// field, in descending order. If fewer than N entries exist, the returned slice
// is shorter than N.
func (bm *BurdenMonitor) GetTopNEntries(n int, sortBy SortBy) []*RPCEntry {
	entries := bm.entriesSortedBy(sortBy)
	if len(entries) > n {
		entries = entries[:n]
	}
	return entries
}
```
