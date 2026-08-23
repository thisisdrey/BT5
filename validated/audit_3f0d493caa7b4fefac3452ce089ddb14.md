This confirms the analog structure. The `LoadMonitor` polls the **parent cgroup** stats via `CGroupManager.Stats()`, which aggregates the resource usage of the top-level `hierarchy_root` cgroup that houses *all* per-repository sub-cgroups on the node (`internal/cgroups/manager_linux.go`, `doc/cgroups.md`). The `AdaptiveCalculator` then derives a single, node-wide `BackoffEvent` from this aggregate signal and applies it uniformly to *every* `AdaptiveLimit`, regardless of which RPC or repository caused the pressure.

### Title
Unprivileged resource-pressure amplification forces node-wide concurrency-limit collapse, causing collateral DoS on unrelated repositories - (File: internal/limiter/adaptive_calculator.go)

### Summary
An ordinary, authenticated (or even unauthenticated, where configured) Gitaly client can drive a single node's aggregate cgroup CPU/memory signal past the fixed `CPUThrottledThreshold`/`MemoryThreshold`, forcing the shared `AdaptiveCalculator` to emit a node-wide backoff event. This event is then applied identically to *every* `AdaptiveLimit` tracked by that calculator — including the per-RPC concurrency limits for repositories and RPCs the attacker never touched — instantly halving their allowed concurrency (`limit.Current() * BackoffFactor`) and pushing unrelated, healthy tenants' in-flight/queued requests into `RESOURCE_EXHAUSTED` rejection.

### Finding Description
The `LoadMonitor` polls a single **parent** cgroup (`cgm.currentProcessCgroup()`), which per `doc/cgroups.md` is the "hierarchy_root" cgroup that aggregates CPU/memory statistics across the entire Gitaly process tree, i.e., across all repositories' sub-cgroups (`repos-N`) on that node [1](#0-0) . `newCPUThrottlingCondition` and `newMemoryUsageCondition` fire a backoff event purely from this shared, aggregate signal crossing a static threshold (default 50% CPU-throttle ratio, 60% anonymous-memory ratio) with no attribution to which repository/RPC/tenant caused it [2](#0-1) [3](#0-2) .

`AdaptiveCalculator.calibrateLimits` then takes that single `BackoffEvent` and iterates over **all** registered `limits`, applying the same multiplicative decrease (`limit.Current() * BackoffFactor`, floored at `Min`) to every one of them — the doc comment explicitly states "they all move as a whole" [4](#0-3) , and the implementation confirms this: the same `newLimit` computation is applied per-limit inside a `for _, limit := range c.limits` loop keyed only on the single global `backoffEvent`, not on which RPC/repo triggered it [5](#0-4) .

Each `AdaptiveLimit` is shared globally per-RPC-name across *all* repositories, since `WithConcurrencyLimiters` constructs one `*AdaptiveLimit` per configured `concurrency.RPC` string and reuses it for every repository's `ConcurrencyLimiter` invocation via `limitingKey` (repository path) [6](#0-5) . When the current limit is updated, `ConcurrencyLimiter.NewConcurrencyLimiter`'s `AfterUpdate` hook resizes the semaphore for **every key** in `limitsByKey` (i.e., every repository currently using that RPC) [7](#0-6) .

This mirrors the Shrine bug class precisely: an unprivileged actor pushes a single shared/global metric (LTV in Shrine; aggregate cgroup CPU/memory ratio in Gitaly) past a fixed threshold, flipping a global mode (recovery mode; backoff/AIMD multiplicative-decrease event) that is then mechanically applied to unrelated positions/tenants (other troves; other repositories/RPCs) that had no part in causing the condition, producing collateral damage (forced liquidation; forced request rejection/DoS).

### Impact Explanation
A single client repeatedly issuing resource-intensive but individually-authorized operations (e.g., large `git-upload-pack`/`git-repack`-triggering clones, big pushes) on their own repository can, within the node's `CalibrationInterval` (default 30s), drive the parent cgroup's CPU-throttle ratio or anon-memory ratio over the fixed threshold. This causes the shared calculator to halve concurrency limits for every configured RPC across every repository hosted on that node, causing other tenants' legitimate, low-load requests to be queued and then rejected with `RESOURCE_EXHAUSTED` (`ErrMaxQueueSize`/`ErrMaxQueueTime`) once queue capacity is exceeded [8](#0-7) . Because limits recover only additively (+1 per calibration cycle) but decrease multiplicatively, repeated or sustained triggering keeps the node's global concurrency depressed, amplifying the DoS window well beyond the attacker's own request duration.

### Likelihood Explanation
Triggering CPU throttling or memory pressure via legitimate, unprivileged Git operations (large clone/fetch/push, deep history operations) is a normal and easily reproducible side effect of using Gitaly at scale — no exploit of Git internals or malicious payload is required, only sufficient repository size/traffic, which is within an ordinary user's control (their own repository) but which impacts all other repositories on the same physical/node-level cgroup hierarchy. This is enabled by default in any deployment using cgroups (`doc/cgroups.md`) and adaptive concurrency limiting (`doc/load-management-architecture.md`), making likelihood high in multi-tenant Gitaly nodes.

### Recommendation
Scope backoff events and limit recalibration to the granularity that caused the pressure where feasible (e.g., per repository-group cgroup rather than only the process-wide parent cgroup), or introduce a secondary, per-repository/per-RPC attribution signal (similar to the `BurdenMonitor`/cost-aware admission design already documented) before applying a global multiplicative decrease, so that a single tenant's load cannot uniformly punish unrelated tenants. At minimum, cap how much of the shared concurrency budget a backoff event can remove within one calibration window, and correlate the triggering repository (already trackable in principle via repository-scoped cgroups per `doc/cgroups.md`) to selectively decrease only the limits associated with the offending workload.

### Proof of Concept
1. Configure Gitaly with `[cgroups]` enabled (parent-level CPU/memory limits) and `[[concurrency]]` with `adaptive = true` for one or more RPCs, plus the default `AdaptiveLimiting` thresholds (`cpu_throttled_threshold = 0.5`, `memory_threshold = 0.9` or configured lower).
2. As an unprivileged client with access to Repository A, issue sustained heavy operations (e.g., concurrent large clones/pushes) sufficient to push the parent cgroup's `CPUThrottledDuration` ratio above the configured threshold within one `CalibrationInterval`, as evaluated by `newCPUThrottlingCondition` [2](#0-1) .
3. Observe via `gitaly_concurrency_limiting_*` metrics or logs ("Multiplicative decrease") that `AdaptiveCalculator.calibrateLimits` halves the current limit for **all** registered `AdaptiveLimiter`s, not just the RPC used in step 2 [9](#0-8) .
4. Concurrently, as an unrelated client, issue normal low-volume requests against Repository B (using an RPC governed by one of the now-halved adaptive limits) and observe increased queueing and eventual `RESOURCE_EXHAUSTED` errors (`ErrMaxQueueSize`/`ErrMaxQueueTime`) that would not have occurred absent the attacker's activity on Repository A [8](#0-7) .

### Citations

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

**File:** internal/limiter/condition_memory.go (L19-45)
```go
// newMemoryUsageCondition evaluates to `true` in 2 conditions:
// * The current memory usage exceeds a soft threshold.
// * The cgroup is under OOM.
func newMemoryUsageCondition(threshold float64) loadmonitor.Condition {
	if threshold <= 0.0 {
		threshold = defaultMemoryThreshold
	}

	return loadmonitor.Condition{
		Name: conditionCgroupMemory,
		Fn: func(ctx context.Context, previous, current loadmonitor.Stats, pollInterval time.Duration) (bool, string) {
			if current.CGroup.ParentStats.UnderOOM {
				return true, eventMemoryOOM
			}

			// This check is to avoid a division by 0 below
			if current.CGroup.ParentStats.MemoryLimit == 0 {
				return false, ""
			}

			anonRatio := float64(current.CGroup.ParentStats.TotalAnon) / float64(current.CGroup.ParentStats.MemoryLimit)
			if anonRatio >= threshold {
				return true, eventMemoryAnon
			}
			return false, ""
		},
	}
```

**File:** internal/limiter/adaptive_calculator.go (L46-56)
```go
// AdaptiveCalculator is responsible for calculating the adaptive limits based on additive increase/multiplicative
// decrease (AIMD) algorithm. This method involves gradually increasing the limit during normal process functioning
// but quickly reducing it when an issue (backoff event) occurs. It receives a list of AdaptiveLimiter and a list of
// ResourceWatcher. Although the limits may have different settings (Initial, Min, Max, BackoffFactor), they all move
// as a whole. The caller accesses the current limits via AdaptiveLimiter.Current method.
//
// When the calculator starts, each limit value is set to its Initial limit. Periodically, the calculator polls the
// backoff events from the watchers. The current value of each limit is re-calibrated as follows:
// * limit = limit + 1 if there is no backoff event since the last calibration. The new limit cannot exceed max limit.
// * limit = limit * BackoffFactor otherwise. The new limit cannot be lower than min limit.
//
```

**File:** internal/limiter/adaptive_calculator.go (L255-288)
```go
	for _, limit := range c.limits {
		setting := limit.Setting()

		var newLimit int
		logger := c.logger.WithField("limit_rpc", limit.Name())

		if backoffEvent == nil {
			// Additive increase, one unit at a time
			newLimit = limit.Current() + 1
			if newLimit > setting.Max {
				newLimit = setting.Max
			}
			logger.WithFields(map[string]interface{}{
				"previous_limit": limit.Current(),
				"new_limit":      newLimit,
			}).Debug("Additive increase")
		} else {
			// Multiplicative decrease
			newLimit = int(math.Floor(float64(limit.Current()) * setting.BackoffFactor))
			if newLimit < setting.Min {
				newLimit = setting.Min
			}
			fields := make(map[string]interface{}, len(backoffStatFields)+4)
			for key, value := range backoffStatFields {
				fields[key] = value
			}
			fields["previous_limit"] = limit.Current()
			fields["new_limit"] = newLimit
			fields["condition"] = backoffEvent.ConditionName
			fields["reason"] = backoffEvent.Reason
			logger.WithFields(fields).Info("Multiplicative decrease")
		}
		c.updateLimit(limit, newLimit)
	}
```

**File:** internal/grpc/middleware/limithandler/middleware.go (L172-192)
```go
// WithConcurrencyLimiters sets up middleware to limit the concurrency of
// requests based on RPC and repository
func WithConcurrencyLimiters(cfg config.Cfg) (map[string]*limiter.AdaptiveLimit, map[string]*limiter.AdaptiveLimit, SetupFunc) {
	perRPCLimits := map[string]*limiter.AdaptiveLimit{}
	perRPCLimitsUnauthenticated := map[string]*limiter.AdaptiveLimit{}

	for _, concurrency := range cfg.Concurrency {
		// Create authenticated limiter
		limitName := fmt.Sprintf("perRPC%s", concurrency.RPC)
		if concurrency.Adaptive {
			perRPCLimits[concurrency.RPC] = limiter.NewAdaptiveLimit(limitName, limiter.AdaptiveSetting{
				Initial:       concurrency.InitialLimit,
				Max:           concurrency.MaxLimit,
				Min:           concurrency.MinLimit,
				BackoffFactor: limiter.DefaultBackoffFactor,
			})
		} else {
			perRPCLimits[concurrency.RPC] = limiter.NewAdaptiveLimit(limitName, limiter.AdaptiveSetting{
				Initial: concurrency.Concurrency(),
			})
		}
```

**File:** internal/limiter/concurrency_limiter.go (L171-184)
```go
	// When the capacity of the limiter is updated we also need to update the size of both the queuing tokens as
	// well as the concurrency tokens to match the new size.
	limit.AfterUpdate(func(val int) {
		for _, keyedLimiter := range limiter.limitsByKey {
			if keyedLimiter.queueTokens != nil {
				if semaphore, ok := keyedLimiter.queueTokens.(*resizableSemaphore); ok {
					semaphore.Resize(uint(val + limiter.maxQueueLength))
				}
			}
			if semaphore, ok := keyedLimiter.concurrencyTokens.(*resizableSemaphore); ok {
				semaphore.Resize(uint(val))
			}
		}
	})
```

**File:** internal/limiter/concurrency_limiter.go (L216-234)
```go
	if err := sem.acquire(ctx, limitingKey); err != nil {
		queueTime := time.Since(start)
		switch {
		case errors.Is(err, ErrMaxQueueSize):
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "max_size")
			return nil, structerr.NewResourceExhausted("%w", ErrMaxQueueSize).WithDetail(&gitalypb.LimitError{
				ErrorMessage: err.Error(),
				RetryAfter:   durationpb.New(0),
			})
		case errors.Is(err, ErrMaxQueueTime):
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "max_time")
			return nil, structerr.NewResourceExhausted("%w", ErrMaxQueueTime).WithDetail(&gitalypb.LimitError{
				ErrorMessage: err.Error(),
				RetryAfter:   durationpb.New(0),
			})
		default:
			c.monitor.Dropped(ctx, limitingKey, sem.queueLength(), sem.inProgress(), queueTime, "other")
			return nil, fmt.Errorf("unexpected error when dequeueing request: %w", err)
		}
```
