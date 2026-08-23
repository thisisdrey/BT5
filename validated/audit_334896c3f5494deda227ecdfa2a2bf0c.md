### Title
Adaptive concurrency-limit multiplicative decrease truncates to zero, silently disabling backpressure during overload - ([File: internal/limiter/adaptive_calculator.go])

### Summary
The GMX bug used `Precision.applyFactor` and only guarded against the *rounds-to-zero* edge case for a single-shot check, but never protected against a fee value that, after repeated fractional truncation, could still legitimately reach zero and be silently dropped. Gitaly's `AdaptiveCalculator.calibrateLimits` has the analogous flaw: it repeatedly applies `int(math.Floor(...))` to shrink an admission-control limit under load, and once that computed value reaches `0`, the number `0` is treated by `ConcurrencyLimiter.Limit` as "no limiting at all" — completely disabling the backpressure mechanism at the exact moment the system is under the most stress.

### Finding Description
`AdaptiveCalculator.calibrateLimits` performs a "multiplicative decrease" on every `AdaptiveLimiter` when a `BackoffEvent` is observed: [1](#0-0) 

```go
newLimit = int(math.Floor(float64(limit.Current()) * setting.BackoffFactor))
if newLimit < setting.Min {
    newLimit = setting.Min
}
```

`setting.Min` is a plain `int` field on `AdaptiveSetting`, whose Go zero value is `0` if an operator does not explicitly configure a positive `MinLimit` for an adaptive `[[concurrency]]` entry: [2](#0-1) 

With `Min == 0` (the default when unset), repeated backoff events (e.g. sustained cgroup memory/CPU pressure, which an ordinary client can induce by issuing a burst of expensive fetch/pull/clone RPCs) cause `limit.Current()` to be halved via `floor()` on every calibration cycle: `..., 3, 1, 0`. Once the floored value hits `0`, the clamp `newLimit < setting.Min` (`0 < 0`) is false, so the limit is *set to exactly zero* and stays there — the fractional "should still restrict something" portion is discarded exactly like the GMX funding-fee fraction was discarded.

The value `0` is not a neutral "very small limit" in Gitaly's semantics — it is a documented magic sentinel meaning "concurrency limiting is disabled": [3](#0-2) 

```go
if c.currentLimit() <= 0 {
    return f()
}
```

So the same mechanism that is supposed to shed load during a `BackoffEvent` (triggered by high load/pressure) instead flips into "admit everything unconditionally" once floor-based truncation walks the limit down to zero — precisely when the node is already under critical resource pressure per the documented load-management flow (`AdaptiveCalculator` -> `ConcurrencyLimiter`). [4](#0-3) 

### Impact Explanation
This is a DoS-amplification bug in an RPC-handler resource-limit mechanism reachable purely by ordinary client traffic (repeated fetch/clone/push RPCs that trip cgroup PSI/CPU/memory backoff conditions). Instead of Gitaly progressively throttling concurrency as designed, sustained backoff events cause the adaptive limit to collapse to `0`, which by design means "no limit is applied at all," admitting unlimited concurrent requests for that RPC/repository exactly while the node is already resource-constrained. This defeats the entire purpose of the concurrency-limiter backpressure system described in `doc/backpressure.md` and `doc/load-management-architecture.md`, and can push an already-struggling node into full resource exhaustion / crash (OOM, CPU starvation) — a DoS of the Gitaly node driven by the very mechanism meant to prevent it.

### Likelihood Explanation
Reaching this state requires:
1. An adaptive concurrency limit configured with `Adaptive = true` and no (or a `0`) explicit `MinLimit`.
2. Enough sustained backoff events (from `LoadMonitor` cgroup pressure conditions) to repeatedly halve the limit below 1.

Since `Min` defaults to the Go zero value unless an operator explicitly sets a positive minimum, and adaptive concurrency limiting is a documented, supported configuration mode, this is plausible in real deployments that enable adaptive limits without also setting a floor above zero. The triggering condition (repeated backoff events under real load) is externally influenceable by ordinary clients generating enough concurrent expensive RPCs to sustain cgroup pressure notifications.

### Recommendation
Do not allow the multiplicative-decrease calculation to reach or clamp to a value that has the special "disabled" meaning unless the operator has explicitly opted into disabling the limiter. Concretely:
- Enforce a hard floor of `1` for adaptive concurrency limits (distinct from the operator-configurable `Min`), so that `0` can only ever be reached via explicit static configuration (`Initial = 0`), never via calibration.
- Alternatively, track the fractional remainder across calibration cycles (analogous to the GMX fix) so a limit is only reduced to `0` intentionally, not as an artifact of repeated `floor()` truncation.

### Proof of Concept
1. Configure an adaptive `[[concurrency]]` limit (e.g. for `PostUploadPackWithSidechannel`) with `adaptive = true`, `initial_limit = 8`, `max_limit = 100`, and no `min_limit` set (defaults to `0`), `BackoffFactor = 0.5` (the default).
2. Drive enough concurrent fetch/clone RPCs from an ordinary client to sustain cgroup memory/CPU pressure long enough to trigger 4+ consecutive `BackoffEvent`s (8 → 4 → 2 → 1 → 0, via `int(math.Floor(current * 0.5))`).
3. Observe that once `AdaptiveCalculator.calibrateLimits` computes `newLimit = 0`, the clamp `0 < setting.Min(0)` is false, so the limit is permanently set to `0`.
4. Any subsequent RPC call into `ConcurrencyLimiter.Limit` for that key hits `if c.currentLimit() <= 0 { return f() }` and bypasses all concurrency limiting/queueing, admitting unlimited concurrent requests while the node remains under the load condition that triggered the backoff in the first place.

### Citations

**File:** internal/limiter/adaptive_calculator.go (L271-276)
```go
		} else {
			// Multiplicative decrease
			newLimit = int(math.Floor(float64(limit.Current()) * setting.BackoffFactor))
			if newLimit < setting.Min {
				newLimit = setting.Min
			}
```

**File:** internal/limiter/adaptive_limit.go (L7-13)
```go
// AdaptiveSetting is a struct that holds the configuration parameters for an adaptive limiter.
type AdaptiveSetting struct {
	Initial       int
	Max           int
	Min           int
	BackoffFactor float64
}
```

**File:** internal/limiter/concurrency_limiter.go (L207-209)
```go
	if c.currentLimit() <= 0 {
		return f()
	}
```

**File:** doc/load-management-architecture.md (L150-159)
```markdown
            alt Healthy
                LM->>AC: No event
                AC->>CL: limit += 1
            else Backoff severity
                LM->>AC: BackoffEvent
                AC->>CL: limit = floor(limit x 0.5)
            else Critical severity
                LM->>AC: BackoffEvent
                AC->>CL: limit = floor(limit x 0.5)
                LM->>LS: CriticalEvent
```
