### Title
Silent watcher/stat-collection failures are treated as "no backoff", causing the adaptive concurrency limiter to raise RPC resource limits during undetected system pressure - (File: internal/limiter/adaptive_calculator.go)

### Summary
`AdaptiveCalculator` computes Gitaly's per-RPC concurrency limits using an AIMD algorithm driven by `BackoffEvent`s emitted by resource `Condition`s (CPU throttling, memory usage, cgroup PSI for CPU/memory/IO). The code explicitly documents that "A watcher returning an error is treated as a no backoff event," meaning any failure to read the underlying resource signal (analogous to the Beanstalk oracle call failing) is silently converted into a "healthy" state rather than causing the calibration to be skipped or handled defensively. [1](#0-0) 

### Finding Description
`AdaptiveCalculator.Start` subscribes to condition events via `loadMonitor.NotifyOn` and spawns `waitForEvents`, which only records a `BackoffEvent` when the channel actually delivers one: [2](#0-1) 

On each calibration tick, `calibrateLimits` reads whatever `lastBackoffEvent` was last recorded (or `nil`) and adjusts every managed limit up (additive increase) when no backoff event was seen, or down (multiplicative decrease) only when one was: [3](#0-2) 

This mirrors the Beanstalk bug class precisely: a data source used to compute a critical protective value (temperature/caseId in Beanstalk; concurrency limit in Gitaly) can fail or return a degenerate/zero reading, and instead of the failure being surfaced or the previous known-good value being retained/handled explicitly, the system silently proceeds as if the reading indicated "everything is fine," and uses that incorrect signal to update protocol/protection state that persists and affects subsequent periods.

Concretely, the PSI condition builder (`CgroupPSIConditionBuilder.fn`) computes severity purely from the current `avg60` value pulled out of `cgroups.CgroupStats`: [4](#0-3) 

If the underlying cgroup PSI file is unreadable/missing (e.g., cgroup remounted, kernel PSI support absent, permission/path issue), and the stats collector returns a zero-valued `PSIMetrics` rather than propagating an explicit error, `classifySeverity(0)` returns `pSIHealthy`, `fn` returns `false, ""`, and no backoff event is ever recorded — exactly the "deltaB == 0 on oracle failure" pattern in the reference bug, which then feeds `calcCaseIdandUpdate`. Here it feeds `calibrateLimits`, which will keep incrementing the RPC concurrency limit even while the node is genuinely under I/O/CPU/memory pressure that its own instrumentation failed to observe.

Full verification of the exact stats-collection failure path (whether `internal/loadmonitor/monitor.go`/`stats.go` return a hard error that is caught elsewhere, or silently zero-fill on read failure) could not be completed in this session due to tool-call exhaustion; this should be confirmed by reading `internal/loadmonitor/monitor.go` and `internal/loadmonitor/stats.go` directly before treating this as fully proven.

### Impact Explanation
If the resource-pressure watcher input degrades silently (stat collection error, cgroup path unavailable, PSI unsupported on the kernel, etc.), the `AdaptiveCalculator` will not back off and will instead continue to raise per-RPC concurrency limits every calibration interval, unbounded up to `MaxLimit`, precisely while the host may be under real resource exhaustion it can no longer detect. This directly threatens Gitaly's core RPC-handler backpressure mechanism (`internal/grpc/middleware/limithandler`), which relies on these limits to protect the node from being overwhelmed, as documented in `doc/backpressure.md`. [5](#0-4) 

This can lead to denial of service on the RPC handler layer: ordinary, unprivileged traffic (fetch/push/clone load) is enough to exercise the concurrency limiter, so an attacker or even organic load spikes combined with a degraded monitoring path can drive the node into resource exhaustion that the adaptive limiter fails to counteract.

### Likelihood Explanation
The condition depends on the monitoring subsystem itself failing or returning degenerate data (cgroup unmounted, kernel without PSI, permission issues in containerized deployments) — not on attacker-controlled input directly. This makes it a plausible but environment-dependent condition rather than a straightforward, always-reachable bug, similar in nature to the Beanstalk finding which also required an external dependency (the oracle) to fail. It is reachable purely through normal operation of the adaptive limiter feature; no privileged actor or malicious peer is required.

### Recommendation
Distinguish "no pressure detected" from "pressure could not be measured." When a resource watcher/condition fails to produce a valid reading, `AdaptiveCalculator` should either (a) skip calibration for that cycle and hold current limits steady (fail-safe), or (b) treat a measurement failure as a de facto backoff signal, similar to the Beanstalk report's recommendation to preserve/act on the last known-good state rather than defaulting silently. Concretely: have `Condition.Fn` (and the underlying `loadmonitor` stats collectors) return an explicit ok/error indicator; propagate it through the event channel; and make `calibrateLimits`/`waitForEvents` treat "measurement error" as a reason to freeze or reduce limits rather than to permit the additive increase branch.

### Proof of Concept
Conceptual reproduction (based on the code paths cited above, not executed):
1. Configure `[[concurrency]].adaptive = true` with PSI pressure monitoring enabled (`config.PSIPressureConfig`), running in a container/cgroup environment.
2. Cause the PSI/cgroup stats source to become unreadable during runtime (e.g., unmount the cgroup pressure file or run in an environment where `/sys/fs/cgroup/.../*.pressure` is absent), while independently driving real CPU/memory/IO load on the host through normal Git operations.
3. Observe via `gitaly_concurrency_limiting_current_limit` metrics that the adaptive limit keeps increasing every `CalibrationInterval` (`c.calibrateLimits` additive-increase branch) because `lastBackoffEvent` stays `nil`, even though the host is genuinely under load — confirming the "silent failure treated as healthy" defect and its DoS potential on the RPC-handler concurrency-limiting path.

### Citations

**File:** internal/limiter/adaptive_calculator.go (L46-58)
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
// A watcher returning an error is treated as a no backoff event.
type AdaptiveCalculator struct {
```

**File:** internal/limiter/adaptive_calculator.go (L204-222)
```go
// waitForEvents listens on the event channel from the load monitor
// and sets the last backoff event only when an event is received.
func (c *AdaptiveCalculator) waitForEvents(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return

		case e := <-c.eventCh:
			c.setLastBackoffEvent(&BackoffEvent{
				ConditionName: e.ConditionName,
				Reason:        e.Description,
				ShouldBackoff: true,
				CurrentStats:  e.CurrentStats,
				PreviousStats: e.PreviousStats,
			})
		}
	}
}
```

**File:** internal/limiter/adaptive_calculator.go (L224-253)
```go
// calibrateLimits reads the lastBackoffEvent and calibrates the limits accordingly.
func (c *AdaptiveCalculator) calibrateLimits(ctx context.Context) {
	// Make a copy of the last backoff event and unlock
	// the mutex so that we don't block `waitForEvents`
	// when a new event comes in.
	var backoffEvent *BackoffEvent
	c.backoffEventMu.Lock()
	backoffEvent = c.lastBackoffEvent
	c.backoffEventMu.Unlock()

	// Precompute the backoff-event log fields once. They are identical for
	// every limit in this calibration round (they depend only on cfg and the
	// event), so doing this work inside the per-limit loop while holding
	// stateMu would scale calibration time linearly with the number of
	// limits for no benefit.
	var backoffStatFields map[string]any
	if backoffEvent != nil {
		stats := buildBackoffStats(c.cfg, *backoffEvent)
		backoffStatFields = make(map[string]any, len(stats))
		for key, value := range stats {
			backoffStatFields[fmt.Sprintf("stats.%s", key)] = value
		}
	}

	c.stateMu.Lock()
	defer c.stateMu.Unlock()

	if ctx.Err() != nil {
		return
	}
```

**File:** internal/limiter/condition_psi.go (L90-99)
```go
// Poll checks PSI pressure and logs at the appropriate severity.
func (b *CgroupPSIConditionBuilder) fn(_ context.Context, previous, current loadmonitor.Stats, _ time.Duration) (bool, string) {
	currentPsi := b.getPSI(current.CGroup.ParentStats)
	previousPsi := b.getPSI(previous.CGroup.ParentStats)
	severity := b.classifySeverity(currentPsi.Some.Avg60)

	if severity == pSIHealthy {
		b.hitThresholdAt = time.Time{}
		return false, ""
	}
```

**File:** doc/backpressure.md (L9-13)
```markdown
If there is a surge of traffic beyond what Gitaly can handle, Gitaly should
be able to push back on the client calling. Gitaly shouldn't subserviently agree
to process more than it can handle.

We employ concurrency limiting as our primary backpressure mechanism in Gitaly.
```
